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


class TenantAuxiliaryVisionTest(TempRoot):
    """Images ride a Private TEE vision model, not the text model (ODL-341-F1).

    An explicit ``auxiliary.vision`` backend is what makes Hermes route
    ``vision_analyze`` to the auxiliary lane (``decide_image_input_mode``
    answers ``text``) and what names the model on that lane. With the key
    absent the lane resolves to ``model.default`` — the text model — which
    returns an empty description at this token budget.

    The pin must be a managed block so a re-run writes it, and must keep the
    keys Hermes writes under the same block (``auxiliary.compression.*``).
    """

    VISION = "Qwen/Qwen3-VL-30B-A3B-Instruct"

    def setUp(self) -> None:
        super().setUp()
        self.mod = load_module(INSTALLER, "odile_install_aux_vision")
        self.layout = tenant_layout_for(self.mod, self.tmp / "root")
        self.seed = {"slug": "odl-test", "instance_id": "i-test",
                     "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated"}
        self.text = self.mod.render_tenant_config(self.layout, self.seed)
        self.preamble, self.blocks = self.mod.split_blocks(self.text)

    def six_block_tenant(self, auxiliary: str = "") -> str:
        """The config this program wrote before this card: six blocks, no auxiliary."""
        order = ("model", "agent", "custom_providers", "dashboard", "webapp", "onboarding")
        lines = [*self.preamble, "_config_version: 52"]
        lines += [line for key in order for line in self.blocks[key]]
        lines += self.blocks["display"]
        if auxiliary:
            lines += auxiliary.splitlines()
        return "\n".join(lines) + "\n"

    def test_the_template_pins_the_private_tee_vision_model(self) -> None:
        self.assertIn("auxiliary", self.blocks, "the tenant config carries no auxiliary block")
        self.assertIn(f"model: {self.VISION}",
                      settings_of(self.mod, self.blocks["auxiliary"]))

    def test_the_block_carries_only_the_model_id(self) -> None:
        # No key, no path, no second value: the model id is the whole pin.
        self.assertEqual(settings_of(self.mod, self.blocks["auxiliary"]),
                         ["auxiliary:", "vision:", f"model: {self.VISION}"])

    def test_auxiliary_is_a_managed_block(self) -> None:
        self.assertIn("auxiliary", self.mod.MANAGED_KEYS)

    def test_the_header_comment_names_every_managed_block(self) -> None:
        # The header rides the `_config_version` block once rendered (the first
        # top-level key ends the preamble), so read the whole text.
        listed = [line.split("*")[1].strip().split()[0]
                  for line in self.text.splitlines() if line.startswith("#   * ")]
        self.assertEqual(sorted(listed), sorted(self.mod.MANAGED_KEYS))
        self.assertIn("Seven managed blocks", self.text)

    def test_a_six_block_tenant_gains_the_pin_and_keeps_compression(self) -> None:
        old = self.six_block_tenant(
            "auxiliary:\n  compression:\n    enabled: true\n"
            "    model: vendor/compress-1")
        merged = self.mod.merge_config(old, self.text)
        self.assertIsNotNone(merged, "the upgrade read as converged and wrote nothing")
        _, blocks = self.mod.split_blocks(merged)
        self.assertIn(f"model: {self.VISION}", settings_of(self.mod, blocks["auxiliary"]))
        self.assertIn("  compression:", merged)
        self.assertIn("    enabled: true", merged)
        # `compression.model` sits at the same depth as `vision.model`: the
        # merge must tell the two paths apart and keep Hermes's value.
        self.assertIn("    model: vendor/compress-1", merged)
        self.assertIn("_config_version: 52", merged)
        self.assertEqual(merged.count("auxiliary:"), 1)
        self.assertIsNone(self.mod.merge_config(merged, self.text),
                          "the upgrade does not converge on the second read")

    def test_a_frontier_vision_pin_is_replaced_and_compression_stays(self) -> None:
        # The pin was briefly a Frontier-tier model, which is not a TEE.
        old = self.six_block_tenant(
            "auxiliary:\n  vision:\n    model: anthropic/claude-haiku-4-5\n"
            "  compression:\n    enabled: true")
        merged = self.mod.merge_config(old, self.text)
        self.assertIsNotNone(merged)
        _, blocks = self.mod.split_blocks(merged)
        self.assertEqual([line for line in settings_of(self.mod, blocks["auxiliary"])
                          if line.startswith("model:")], [f"model: {self.VISION}"])
        self.assertNotIn("claude-haiku", merged)
        self.assertIn("    enabled: true", merged)


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


class GatewayKeyOrderTest(TempRoot):
    """A key file the gateway never read is a restart, not an expired key.

    Measured on the demo tenant's first boot (t_bf6092d7): ``odile-agent`` is
    enabled in the image and starts at boot, the release installer writes
    ``/etc/odile/key.env`` later in the same boot, and the smoke's chat turn
    answered ``Provider said: HTTP 401: Invalid or expired API key``. Restarting
    the unit by hand fixed it. So the key file is a gateway start input like the
    unit, the config and the ``.env`` — and a first boot can leave the unit
    ``activating``, where the old code ran ``systemctl start`` (a no-op on the
    process already running) instead of ``restart``.

    The run's fake ``systemctl`` never starts a process: it reports the state
    the boot left behind (active, a main PID, that PID's environment), which is
    what decides whether the installer converged.
    """

    def setUp(self) -> None:
        super().setUp()
        self.mod = load_module(INSTALLER, "odile_install_copy_key_order")
        self.layout = tenant_layout_for(self.mod, self.tmp / "root")
        self.layout.unit_target.parent.mkdir(parents=True, exist_ok=True)
        self.layout.unit_target.write_text("unit\n", encoding="utf-8")
        self.proc = self.tmp / "proc"
        (self.proc / "4242").mkdir(parents=True)
        self.calls: list[list[str]] = []
        self.state = {"active": "active", "pid": "4242"}
        self.key_present_at_restart: list[bool] = []

    def fake_run(self, argv, *_, check=False, **__):
        self.calls.append(list(argv))
        if argv[:2] == ["systemctl", "restart"]:
            self.key_present_at_restart.append(self.layout.key_env.is_file())
            # A restart is the unit's next exec: the fake reads the file the
            # real systemd would read through EnvironmentFile=.
            if self.layout.key_env.is_file():
                self.write_gateway_environ(self.mod.env_values(
                    self.layout.key_env.read_text(encoding="utf-8")))
        stdout = ""
        if argv[:2] == ["systemctl", "is-enabled"]:
            stdout = "enabled\n"
        elif argv[:2] == ["systemctl", "is-active"]:
            stdout = f"{self.state['active']}\n"
        elif argv[:2] == ["systemctl", "show"]:
            stdout = f"{self.state['pid']}\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    def write_gateway_environ(self, values: dict[str, str]) -> None:
        """What the gateway's process holds at exec, as /proc/<pid>/environ."""
        body = b"".join(f"{name}={value}\0".encode("utf-8") for name, value in values.items())
        (self.proc / self.state["pid"] / "environ").write_bytes(body)

    def write_key(self) -> bool:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return self.mod.ensure_key_env(
                self.layout, {"near_api_key": "near-fixture-" + "k" * 32}, False)

    def converge(self, *, units_changed: bool = False, **kwargs):
        out = io.StringIO()
        with mock.patch.object(self.mod, "run", self.fake_run), \
                mock.patch.object(self.mod, "wait_for_webapp", lambda *a, **k: True), \
                mock.patch.object(self.mod, "PROC", self.proc), \
                contextlib.redirect_stdout(out):
            changed = self.mod.ensure_systemd_services(
                self.layout, False, units_changed, **kwargs)
        return changed, out.getvalue()

    def test_the_key_write_is_what_restarts_the_gateway(self) -> None:
        # The running gateway is the boot's own: it started before the file.
        self.write_gateway_environ({"HOME": "/home/tenant"})
        self.assertTrue(self.write_key())
        moved, reason = self.mod.gateway_start_inputs(self.layout, key_changed=True)
        self.assertTrue(moved)
        self.assertIn(str(self.layout.key_env), reason)
        changed, text = self.converge(gateway_changed=moved, gateway_reason=reason)
        self.assertIn(["systemctl", "restart", "odile-agent"], self.calls)
        self.assertIn(str(self.layout.key_env), text)
        self.assertEqual(self.key_present_at_restart, [True])
        self.assertTrue(changed)

    def test_a_gateway_running_without_the_key_is_restarted_even_when_nothing_moved(
            self) -> None:
        self.assertTrue(self.write_key())          # the file is right …
        self.write_gateway_environ({"HOME": "/home/tenant"})   # … the process is not
        changed, text = self.converge(gateway_changed=False)
        self.assertIn(["systemctl", "restart", "odile-agent"], self.calls)
        self.assertIn("HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY", text)
        self.assertTrue(changed)

    def test_a_starting_unit_with_a_changed_input_is_restarted_not_started(self) -> None:
        self.state["active"] = "activating"
        self.write_gateway_environ({"HOME": "/home/tenant"})
        self.converge(units_changed=True)
        self.assertIn(["systemctl", "restart", "odile-agent"], self.calls)
        self.assertNotIn(["systemctl", "start", "odile-agent"], self.calls)

    def test_a_converged_gateway_holding_the_key_is_left_alone(self) -> None:
        self.assertTrue(self.write_key())
        self.write_gateway_environ(self.mod.env_values(
            self.layout.key_env.read_text(encoding="utf-8")))
        changed, text = self.converge(gateway_changed=False)
        self.assertFalse(changed)
        self.assertNotIn(["systemctl", "restart", "odile-agent"], self.calls)
        self.assertNotIn(["systemctl", "start", "odile-agent"], self.calls)
        self.assertIn("same         odile-agent active", text)


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
        # The script also checks the boot's access report (ODL-947). This class
        # is about the installer argv, so give it a healthy report and an
        # `active` unit: a fresh file and a stub `systemctl` in PATH.
        report = self.tmp / "access-report.txt"
        report.write_text("read from your machine\n", encoding="utf-8")
        bindir = self.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        systemctl = bindir / "systemctl"
        systemctl.write_text("#! /bin/sh\nprintf 'active\\n'\nexit 0\n", encoding="utf-8")
        systemctl.chmod(0o755)
        env = {
            "PATH": str(bindir) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
            "ODILE_RELEASE_CMDLINE": str(self.cmdline),
            "ODILE_RELEASE_PIN": str(self.pin),
            "ODILE_RELEASE_ATTEST": str(self.attest),
            "ODILE_RELEASE_INSTALLER": str(self.installer),
            "ODILE_RELEASE_SIGNATURE": str(self.signature),
            "ODILE_RELEASE_PUBKEY": str(self.pubkey),
            "ODILE_RELEASE_LOG": str(self.log),
            "ODILE_RELEASE_DONE": str(self.done),
            "ODILE_RELEASE_REPORT": str(report),
            "ODILE_RELEASE_UNIT": "odile-access-report",
            "ODILE_RELEASE_REPORT_WAIT": "0",
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


class InstallerVerifySurfaceTest(unittest.TestCase):
    """The /verify program and template the signed installer carries (t_4fcd465f).

    The page moved off the first-visit service: the installer now embeds
    `verify/verify.py` and `templates/verify.html`, places them under
    `/usr/local/lib/odile/`, and splices one exact Caddy route to the program's
    own loopback port. These tests load the installer and the generator and
    check the copy, the pin, the placement and a one-byte edit.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = load_module(INSTALLER, "odile_install_verify_copy_under_test")
        cls.generator = load_module(ROOT / "installer" / "embed_verify.py",
                                    "embed_verify_under_test")
        cls.program = (ROOT / "verify" / "verify.py").read_bytes()
        cls.template = (ROOT / "templates" / "verify.html").read_bytes()

    def scratch(self, tmp: str, port: int):
        return self.installer.Layout.scratch(
            root=Path(tmp), user=pwd.getpwuid(os.geteuid()).pw_name,
            webapp_port=port, public_url=f"http://verify.test:{port}",
        )

    def test_the_embedded_program_and_template_are_the_reviewed_files(self):
        self.assertEqual(self.installer.VERIFY_PROGRAM.encode("utf-8"), self.program)
        self.assertEqual(self.installer.VERIFY_TEMPLATE.encode("utf-8"), self.template)
        self.assertEqual(self.installer.VERIFY_PROGRAM_SHA256,
                         hashlib.sha256(self.program).hexdigest())
        self.assertEqual(self.installer.VERIFY_TEMPLATE_SHA256,
                         hashlib.sha256(self.template).hexdigest())

    def test_the_generator_renders_exactly_the_block_the_installer_carries(self):
        block = self.generator.render_block(
            self.program, self.template,
            (ROOT / "installer" / "access_report.sh").read_bytes(),
            (ROOT / "scripts" / "verify_and_unlock.sh").read_bytes(),
            (ROOT / "scripts" / "audit.py").read_bytes())
        self.assertIn(block, INSTALLER.read_text(encoding="utf-8"))

    def test_the_placement_writes_0755_and_0644_and_a_second_run_reads_same(self):
        with tempfile.TemporaryDirectory(prefix="odile-verify-surface-") as tmp:
            layout = self.scratch(tmp, 9158)
            self.assertTrue(self.installer.ensure_verify_surface(layout, False))
            program = Path(tmp) / "usr/local/lib/odile/verify.py"
            template = Path(tmp) / "usr/local/lib/odile/verify.html"
            self.assertEqual(program.stat().st_mode & 0o777, 0o755)
            self.assertEqual(template.stat().st_mode & 0o777, 0o644)
            self.assertEqual(program.read_bytes(), self.program)
            self.assertEqual(template.read_bytes(), self.template)
            self.assertFalse(self.installer.ensure_verify_surface(layout, False))

    def test_a_one_byte_edit_to_either_copy_is_refused(self):
        for name in ("VERIFY_PROGRAM", "VERIFY_TEMPLATE"):
            with self.subTest(constant=name), \
                    tempfile.TemporaryDirectory(prefix="odile-verify-edit-") as tmp:
                layout = self.scratch(tmp, 9159)
                original = getattr(self.installer, name)
                try:
                    setattr(self.installer, name, original.replace("a", "b", 1))
                    with self.assertRaises(SystemExit):
                        self.installer.ensure_verify_surface(layout, False)
                finally:
                    setattr(self.installer, name, original)
                template_path = Path(tmp) / "usr/local/lib/odile/verify.html"
                self.assertFalse(template_path.exists(), "the tampered template was written")
                if name == "VERIFY_PROGRAM":
                    self.assertFalse((Path(tmp) / "usr/local/lib/odile/verify.py").exists(),
                                     "the tampered program was written")

    def test_the_caddy_splice_names_the_program_port_and_not_the_first_visit_one(self):
        handles = (self.installer.CADDY_VERIFY_HANDLE.format(port=8098)
                   + self.installer.CADDY_FIRST_VISIT_HANDLE.format(port=8091))
        self.assertIn("handle /verify {\n\t\theader Cache-Control \"no-store\"\n"
                      "\t\treverse_proxy 127.0.0.1:8098", handles)
        self.assertNotIn("handle /verify", self.installer.CADDY_FIRST_VISIT_HANDLE)
        block = self.installer.CADDY_SITE_TEMPLATE.format(
            begin="odile-odl-test", fqdn="odl-test.odile.fyi", user="odile",
            webapp_port=9120, var=Path("/var/lib/odile"), first_visit_handle=handles)
        self.assertLess(block.index("handle /attestation"),
                        block.index("handle /verify {"))
        self.assertLess(block.index("handle /verify {"),
                        block.index("handle {\n\t\treverse_proxy 127.0.0.1:9120"))
        classic = self.installer.CADDY_SITE_TEMPLATE.format(
            begin="odile-odl-test", fqdn="odl-test.odile.fyi", user="odile",
            webapp_port=9120, var=Path("/var/lib/odile"), first_visit_handle="")
        self.assertNotIn("handle /verify", classic)


if __name__ == "__main__":
    unittest.main()
