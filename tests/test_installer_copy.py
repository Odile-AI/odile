"""The installer's copy of the tenant's config, its units and its boot path.

``installer/odile_install.py`` names this file for the backup-unit parity test:
the installer and ``scripts/backup.py`` render the same two units, and a drift
between them is a tenant whose backup unit differs from the one the program
writes. It also holds the behaviours the live tenant's 400 paid for
(t_d95b8c13):

* the tenant config names the machine's public host in the `webapp` block the
  Webapp listener reads (`webapp.public_url` sets its `listener_public_url`)
  and in the `dashboard` block — which is not the Webapp's fallback: measured
  2026-11-15 on the pinned Hermes, `dashboard.public_url` alone answers 400
  `Invalid Host header`;
* a failed optional oneshot (``odile-access-report``) does not abort the run
  before the agent restart that makes a rewritten config live;
* the boot-time release setup passes the recovery confirmation and the
  machine's public URL, so a converged install ends ``=== rc=0`` and keeps the
  host the owner declared.

The installer is a program, not a library, so each suite loads it the way the
other suites do: register the module in ``sys.modules`` before ``exec_module``
or the frozen dataclasses fail to resolve their annotations.

The second half is ODL-218 follow-up's guest export program: the installer
carries `odilectl/offboard.py`'s `EXPORT_PROGRAM` byte for byte and places
it at `/usr/local/bin/odile-export`, so a fresh tenant has the command the
drill placed by hand.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import offboard  # noqa: E402  (the program's one owner)

INSTALLER = ROOT / "installer" / "odile_install.py"
GENERATOR = ROOT / "installer" / "embed_export.py"
BACKUP = ROOT / "scripts" / "backup.py"
SETUP = ROOT / "deploy" / "image" / "odile-release-setup.sh"
MEASUREMENT = "ab" * 48


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def tenant_layout_for(module, root: Path, public_url: str = "https://odl-test.odile.fyi"):
    return module.Layout.scratch(root=root, user="tenant", webapp_port=9130,
                                 public_url=public_url)


def settings_of(module, block: list[str]) -> list[str]:
    """The block's setting lines, flattened past their indentation."""
    return [module.setting_line(line).strip() for line in module.settings(block)]


class TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-installer-copy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class BackupUnitParityTest(TempRoot):
    """The installer's templates and scripts/backup.py must render one unit."""

    def test_the_backup_service_and_timer_are_byte_identical(self) -> None:
        installer = load_module(INSTALLER, "odile_install_copy_parity")
        backup = load_module(BACKUP, "odile_backup_copy_parity")
        values = dict(
            script=Path("/usr/local/lib/odile/backup.py"),
            repo_file=Path("/etc/odile/backup-repo.txt"),
            repo_arg="",
            recovery=Path("/etc/odile/recovery.txt"),
            home=Path("/home/tenant"),
            key=Path("/home/tenant/.ssh/id_ed25519"),
            known_hosts=Path("/home/tenant/.ssh/known_hosts"),
            status=Path("/var/lib/odile/backup-status.json"),
            slug="odl-test",
            url="https://odile.fyi/heartbeat",
            heartbeat_dir=Path("/home/tenant/.ssh"),
        )
        self.assertEqual(
            installer.BACKUP_SERVICE_TEMPLATE.format(**values),
            backup.SERVICE_TEMPLATE.format(**values))
        self.assertEqual(installer.BACKUP_TIMER_TEMPLATE, backup.TIMER_TEMPLATE)


class TenantConfigTrustedHostTest(TempRoot):
    """Both blocks name the host; the Webapp reads `webapp.public_url`."""

    def setUp(self) -> None:
        super().setUp()
        self.mod = load_module(INSTALLER, "odile_install_copy_config")
        self.layout = tenant_layout_for(self.mod, self.tmp / "root")
        self.seed = {"slug": "odl-test", "instance_id": "i-test",
                     "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated"}
        self.text = self.mod.render_tenant_config(self.layout, self.seed)
        self.preamble, self.blocks = self.mod.split_blocks(self.text)

    def test_the_config_declares_the_host_the_gateway_trusts(self) -> None:
        self.assertIn("dashboard", self.blocks,
                      "the tenant config carries no dashboard block")
        self.assertIn("public_url: https://odl-test.odile.fyi",
                      settings_of(self.mod, self.blocks["dashboard"]))

    def test_the_webapp_listener_declares_the_same_host(self) -> None:
        self.assertIn("public_url: https://odl-test.odile.fyi",
                      settings_of(self.mod, self.blocks["webapp"]))

    def test_dashboard_is_a_managed_block(self) -> None:
        self.assertIn("dashboard", self.mod.MANAGED_KEYS)

    def test_a_five_block_tenant_gains_the_block_and_keeps_its_own(self) -> None:
        # The config this program wrote before this card: five managed blocks,
        # the display skin, and one key Hermes or the owner added.
        order = ("model", "agent", "custom_providers", "webapp", "onboarding")
        old = "\n".join(
            [*self.preamble, "_config_version: 52",
             *[line for key in order for line in self.blocks[key]],
             *self.blocks["display"],
             "telegram:", "  bot_token: owner-value", ""])
        merged = self.mod.merge_config(old, self.text)
        self.assertIsNotNone(merged, "the upgrade read as converged and wrote nothing")
        _, blocks = self.mod.split_blocks(merged)
        self.assertIn("public_url: https://odl-test.odile.fyi",
                      settings_of(self.mod, blocks["dashboard"]))
        self.assertIn("_config_version: 52", merged)
        self.assertIn("  bot_token: owner-value", merged)
        self.assertIn("display:\n  skin: odile", merged)

    def test_an_owner_dashboard_block_keeps_its_keys(self) -> None:
        order = ("model", "agent", "custom_providers", "webapp", "onboarding")
        old = "\n".join(
            [*self.preamble, "_config_version: 52",
             *[line for key in order for line in self.blocks[key]],
             "dashboard:", "  basic_auth:", "    username: owner", ""])
        merged = self.mod.merge_config(old, self.text)
        self.assertIsNotNone(merged)
        _, blocks = self.mod.split_blocks(merged)
        self.assertIn("public_url: https://odl-test.odile.fyi",
                      settings_of(self.mod, blocks["dashboard"]))
        self.assertIn("  basic_auth:", merged)
        self.assertIn("    username: owner", merged)

    def test_the_upgrade_converges_on_the_second_read(self) -> None:
        order = ("model", "agent", "custom_providers", "webapp", "onboarding")
        old = "\n".join(
            [*self.preamble, "_config_version: 52",
             *[line for key in order for line in self.blocks[key]], ""])
        merged = self.mod.merge_config(old, self.text)
        self.assertIsNotNone(merged)
        self.assertIsNone(self.mod.merge_config(merged, self.text))


class OptionalUnitToleranceTest(TempRoot):
    """A failed report must not stop the agent restart that makes config live."""

    def setUp(self) -> None:
        super().setUp()
        self.mod = load_module(INSTALLER, "odile_install_copy_units")
        self.layout = tenant_layout_for(self.mod, self.tmp / "root")
        self.layout.access_report_unit.parent.mkdir(parents=True, exist_ok=True)
        self.layout.access_report_unit.write_text("unit\n", encoding="utf-8")
        self.calls: list[list[str]] = []

    def fake_run(self, argv, *_, check=False, **__):
        self.calls.append(list(argv))
        unit = argv[2] if len(argv) > 2 else ""
        status = 0
        stdout = ""
        stderr = ""
        if argv[:2] == ["systemctl", "is-enabled"]:
            stdout = "enabled\n"
        elif argv[:2] == ["systemctl", "is-active"]:
            stdout = "active\n" if unit == "odile-agent" else "inactive\n"
        elif argv[:3] == ["systemctl", "start", "odile-access-report"]:
            status = 1
            stderr = "Job for odile-access-report.service failed\n"
        if check and status != 0:
            # the real run() raises; the fake must too or the tolerance is untested
            raise RuntimeError(f"{' '.join(argv)} failed ({status}): {stderr.strip()}")
        return subprocess.CompletedProcess(argv, status, stdout=stdout, stderr=stderr)

    def drive(self, **kwargs):
        out = io.StringIO()
        with mock.patch.object(self.mod, "run", self.fake_run), \
                mock.patch.object(self.mod, "wait_for_webapp", lambda *a, **k: True), \
                contextlib.redirect_stdout(out):
            self.mod.ensure_systemd_services(self.layout, False, False, **kwargs)
        return out.getvalue()

    def test_the_agent_is_restarted_after_a_failed_report(self) -> None:
        text = self.drive(gateway_changed=True)
        self.assertIn(["systemctl", "start", "odile-access-report"], self.calls)
        self.assertIn(["systemctl", "restart", "odile-agent"], self.calls)
        self.assertLess(
            self.calls.index(["systemctl", "start", "odile-access-report"]),
            self.calls.index(["systemctl", "restart", "odile-agent"]))
        self.assertIn("odile-access-report", text)
        self.assertIn("FAIL", text)

    def test_a_converged_boot_still_leaves_the_agent_alone(self) -> None:
        text = self.drive(gateway_changed=False)
        self.assertNotIn(["systemctl", "restart", "odile-agent"], self.calls)
        self.assertNotIn(["systemctl", "start", "odile-agent"], self.calls)
        self.assertIn("FAIL", text)

    def test_the_old_three_argument_call_still_starts_the_agent(self) -> None:
        # ensure_systemd_services is called with unit changes only in older
        # call sites; the default keeps their meaning.
        out = io.StringIO()
        with mock.patch.object(self.mod, "run", self.fake_run), \
                mock.patch.object(self.mod, "wait_for_webapp", lambda *a, **k: True), \
                contextlib.redirect_stdout(out):
            self.mod.ensure_systemd_services(self.layout, False, True)
        self.assertIn(["systemctl", "restart", "odile-agent"], self.calls)


class ReleaseSetupInvocationTest(TempRoot):
    """The boot install converges and keeps the machine's public host."""

    def setUp(self) -> None:
        super().setUp()
        self.cmdline = self.tmp / "cmdline"
        self.cmdline.write_text(
            "BOOT_IMAGE=test odile.token=" + "a" * 64
            + " bb.iid=i-test bb.host=odl-test\n", encoding="utf-8")
        self.pin = self.tmp / "pin.json"
        self.pin.write_text(json.dumps({"uki_signed": {"measurement": MEASUREMENT}}),
                            encoding="utf-8")
        self.attest = self.tmp / "attest.json"
        self.attest.write_text(json.dumps({"measurement": MEASUREMENT}), encoding="utf-8")
        self.argv_file = self.tmp / "argv.json"
        self.installer = self.tmp / "installer.py"
        self.installer.write_text(
            "#! /usr/bin/python3\n"
            "import json, sys\n"
            f"json.dump(sys.argv[1:], open({str(self.argv_file)!r}, 'w'))\n"
            "sys.exit(0)\n", encoding="utf-8")
        self.installer.chmod(0o755)
        self.signature = self.tmp / "installer.sig"
        self.signature.write_text("sig\n", encoding="utf-8")
        self.pubkey = self.tmp / "signing-key.pub"
        self.pubkey.write_text("pub\n", encoding="utf-8")
        self.log = self.tmp / "release-setup.log"
        self.done = self.tmp / "state" / "release-setup.done"

    def run_script(self, *argv: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "ODILE_RELEASE_CMDLINE": str(self.cmdline),
            "ODILE_RELEASE_PIN": str(self.pin),
            "ODILE_RELEASE_ATTEST": str(self.attest),
            "ODILE_RELEASE_INSTALLER": str(self.installer),
            "ODILE_RELEASE_SIGNATURE": str(self.signature),
            "ODILE_RELEASE_PUBKEY": str(self.pubkey),
            "ODILE_RELEASE_LOG": str(self.log),
            "ODILE_RELEASE_DONE": str(self.done),
        }
        env.update(extra)
        return subprocess.run(["sh", str(SETUP)] + list(argv),
                              env=env, text=True, capture_output=True, timeout=60)

    def test_the_boot_install_confirms_the_recovery_file(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.argv_file.read_text(encoding="utf-8"))
        self.assertIn("--i-have-copied-it", argv)
        self.assertEqual(argv[argv.index("--public-url") + 1],
                         "https://odl-test.odile.fyi")

    def test_the_owner_declared_public_host_is_kept(self) -> None:
        result = self.run_script(ODILE_RELEASE_PUBLIC_URL="https://demo.odile.fyi")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.argv_file.read_text(encoding="utf-8"))
        self.assertEqual(argv[argv.index("--public-url") + 1], "https://demo.odile.fyi")




class InstallerExportProgramTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = load_module(INSTALLER, "odile_install_copy_under_test")
        cls.generator = load_module(GENERATOR, "embed_export_under_test")
        cls.program = offboard.EXPORT_PROGRAM
        cls.bytes = cls.program.encode("utf-8")
        cls.digest = hashlib.sha256(cls.bytes).hexdigest()

    def scratch(self, tmp: str, port: int):
        return self.installer.Layout.scratch(
            root=Path(tmp), user=pwd.getpwuid(os.geteuid()).pw_name,
            webapp_port=port, public_url=f"http://copy.test:{port}",
        )

    def test_the_embedded_copy_is_the_offboard_program_byte_for_byte(self):
        self.assertEqual(self.installer.EXPORT_PROGRAM_SOURCE, self.program)
        self.assertEqual(self.installer.EXPORT_PROGRAM_SHA256, self.digest)

    def test_the_generator_renders_exactly_the_block_the_installer_carries(self):
        block = self.generator.render_block(self.bytes)
        self.assertIn(block, INSTALLER.read_text(encoding="utf-8"))

    def test_the_copy_is_what_odilectl_export_script_prints(self):
        # `export --script` prints the guest program and reads nothing from a
        # tenant, but the CLI loads its config before the command runs. On a
        # machine with no /root/.odile/config.yaml (CI, a fresh clone) the
        # command exits 5; give it a minimal one so the test measures the
        # bytes, not the host.
        with tempfile.TemporaryDirectory(prefix="odile-export-config-") as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("provider: servury\n", encoding="utf-8")
            config.chmod(0o600)
            result = subprocess.run(
                [sys.executable, "-m", "odilectl", "export", "odl-copy-test",
                 "--script", "--config", str(config)],
                cwd=ROOT, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stdout, self.bytes)

    def test_the_placement_writes_0755_and_a_second_run_reads_same(self):
        with tempfile.TemporaryDirectory(prefix="odile-export-copy-") as tmp:
            layout = self.scratch(tmp, 9148)
            self.assertTrue(self.installer.ensure_export_program(layout, False))
            target = Path(tmp) / "usr/local/bin/odile-export"
            self.assertTrue(target.is_file())
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual(target.read_bytes(), self.bytes)
            self.assertFalse(self.installer.ensure_export_program(layout, False))

    def test_a_one_byte_edit_to_the_copy_is_refused_before_anything_is_written(self):
        with tempfile.TemporaryDirectory(prefix="odile-export-edit-") as tmp:
            layout = self.scratch(tmp, 9149)
            original = self.installer.EXPORT_PROGRAM_SOURCE
            try:
                self.installer.EXPORT_PROGRAM_SOURCE = original.replace(
                    "#!/bin/sh", "#!/bin/sg", 1)
                with self.assertRaises(SystemExit):
                    self.installer.ensure_export_program(layout, False)
            finally:
                self.installer.EXPORT_PROGRAM_SOURCE = original
            self.assertFalse((Path(tmp) / "usr/local/bin/odile-export").exists())

    def test_the_manifest_names_the_program_it_places(self):
        result = subprocess.run([sys.executable, str(INSTALLER), "--manifest"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["export_program_sha256"], self.digest)


if __name__ == "__main__":
    unittest.main()
