"""ODL-348 follow-up: the guest's name comes from `bb.host`, not the build.

The published disk bakes its build tenant's slug into `/etc/hostname` and the
two `/etc/hosts` lines (`deploy/image/sealed-image.sh`). A machine recycled from
tenant A onto tenant B therefore answered `A login:` at the getty with B's key,
B's seed and B's inference key installed. `odile-firstboot` now re-derives the
name from the measured line on every boot; these tests pin the write, the
converged no-op, and the read-only probe.
"""

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIRSTBOOT = ROOT / "deploy" / "image" / "odile-firstboot.py"

BUILD_HOSTS = (
    "127.0.0.1 localhost odile-bench tenant.odile.test\n"
    "10.0.2.15 odile-bench tenant.odile.test\n"
)


def load_firstboot():
    spec = importlib.util.spec_from_file_location("firstboot_under_test_hostname", FIRSTBOOT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % FIRSTBOOT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnsureHostnameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firstboot = load_firstboot()
        self.tmp = tempfile.TemporaryDirectory(prefix="odl348-hostname-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.hostname = self.root / "hostname"
        self.hosts = self.root / "hosts"
        self.hostname.write_text("odile-bench\n", encoding="utf-8")
        self.hosts.write_text(BUILD_HOSTS, encoding="utf-8")
        self.calls: list[str] = []

    def run_once(self, name: str = "odl-348b2c", kernel: bool = False):
        set_kernel = self.calls.append if kernel else None
        return self.firstboot.ensure_hostname(
            name, hostname_path=str(self.hostname), hosts_path=str(self.hosts),
            set_kernel=set_kernel)

    def test_a_fresh_boot_takes_the_lines_slug_over_the_build_slug(self) -> None:
        verb, detail = self.run_once()
        self.assertEqual(verb, "write", detail)
        self.assertEqual(self.hostname.read_text(encoding="utf-8"), "odl-348b2c\n")
        self.assertEqual(self.hosts.read_text(encoding="utf-8"),
                         "127.0.0.1 localhost odl-348b2c tenant.odile.test\n"
                         "10.0.2.15 odl-348b2c tenant.odile.test\n")
        self.assertNotIn("odile-bench", self.hosts.read_text(encoding="utf-8"))

    def test_a_second_boot_writes_nothing(self) -> None:
        self.run_once()
        before = (self.hostname.read_bytes(), self.hosts.read_bytes(),
                  self.hostname.stat().st_mtime_ns, self.hosts.stat().st_mtime_ns)
        verb, detail = self.run_once()
        after = (self.hostname.read_bytes(), self.hosts.read_bytes(),
                 self.hostname.stat().st_mtime_ns, self.hosts.stat().st_mtime_ns)
        self.assertEqual(verb, "same", detail)
        self.assertEqual(before, after)

    def test_the_kernel_name_is_set_once_and_only_when_it_differs(self) -> None:
        self.run_once(kernel=True)
        self.assertEqual(self.calls, ["odl-348b2c"])
        # A converged reboot: the files say the name and the kernel already has
        # it (systemd read /etc/hostname before this unit ran), so nothing moves.
        with mock.patch.object(self.firstboot.socket, "gethostname",
                               return_value="odl-348b2c"):
            verb, detail = self.run_once(kernel=True)
        self.assertEqual(verb, "same", detail)
        self.assertEqual(self.calls, ["odl-348b2c"])

    def test_a_kernel_the_unit_set_does_not_make_the_files_rewrite(self) -> None:
        self.run_once()
        with mock.patch.object(self.firstboot.socket, "gethostname",
                               return_value="odl-348b2c"):
            verb, detail = self.firstboot.ensure_hostname(
                "odl-348b2c", hostname_path=str(self.hostname),
                hosts_path=str(self.hosts), set_kernel=self.calls.append)
        self.assertEqual(verb, "same", detail)
        self.assertIn("kernel same", detail)
        self.assertEqual(self.calls, [])

    def test_lines_that_do_not_name_this_image_are_kept_byte_for_byte(self) -> None:
        extra = "10.9.9.9 nas.lan nas  # the owner's own\n\n"
        self.hosts.write_text(BUILD_HOSTS + extra, encoding="utf-8")
        self.run_once()
        text = self.hosts.read_text(encoding="utf-8")
        self.assertIn("\n10.9.9.9 nas.lan nas  # the owner's own\n\n", text)
        self.assertTrue(text.endswith("\n\n"))

    def test_the_old_name_is_replaced_wherever_it_stands_as_a_field(self) -> None:
        # The image lines plus a line the owner added with the old name: the
        # field is replaced, the line keeps its other names.
        self.hosts.write_text(BUILD_HOSTS + "127.0.0.1 odile-bench localhost\n",
                              encoding="utf-8")
        self.run_once()
        self.assertIn("127.0.0.1 odl-348b2c localhost\n",
                      self.hosts.read_text(encoding="utf-8"))

    def test_an_already_correct_file_with_no_hosts_line_still_converges(self) -> None:
        self.hostname.write_text("ODL-348B2C\n", encoding="utf-8")
        self.hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
        verb, detail = self.run_once("odl-348b2c")
        self.assertEqual(verb, "write", detail)  # stale file bytes differ
        self.assertEqual(self.hostname.read_text(encoding="utf-8"), "odl-348b2c\n")
        self.assertEqual(self.hosts.read_text(encoding="utf-8"),
                         "127.0.0.1 localhost\n")


class CmdlineFactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firstboot = load_firstboot()

    def test_the_host_is_read_from_the_line_not_the_image(self) -> None:
        facts = self.firstboot.cmdline_facts(
            "root=/dev/mapper/root_crypt bb.host=odl-348b2c bb.iid=168020b4")
        self.assertEqual(facts["hostname"], "odl-348b2c")

    def test_an_absent_host_is_an_empty_fact(self) -> None:
        self.assertEqual(self.firstboot.cmdline_facts("bb.iid=one")["hostname"], "")


class MainWiringTests(unittest.TestCase):
    """`main` runs the step before any seed work, and `--check` never writes."""

    def setUp(self) -> None:
        self.firstboot = load_firstboot()
        self.tmp = tempfile.TemporaryDirectory(prefix="odl348-main-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text(
            "root=/dev/mapper/root_crypt bb.host=odl-348b2c bb.iid=168020b4"
            " odile.seed_url=https://test.odile.fyi/seed\n", encoding="utf-8")
        self.hostname = self.root / "hostname"
        self.hosts = self.root / "hosts"
        self.hostname.write_text("odile-bench\n", encoding="utf-8")
        self.hosts.write_text(BUILD_HOSTS, encoding="utf-8")
        self.log = self.root / "firstboot.log"

    def argv(self, *extra: str) -> list[str]:
        return [
            "--cmdline", str(self.cmdline),
            "--state", str(self.root / "state.json"),
            "--seed-cache", str(self.root / "etc" / "seed.json"),
            "--signing-key", str(self.root / "signing.pub"),
            "--installer", str(self.root / "install-stack.sh"),
            "--recovery", str(self.root / "recovery.txt"),
            "--recovery-seed", str(self.root / "recovery-seed.py"),
            "--first-visit-service", str(self.root / "absent.py"),
            "--log", str(self.log),
            "--release-dir", str(self.root / "release"),
            "--installer-copy", str(self.root / "odile-install.sh"),
            "--ssh-root", str(self.root / "ssh"),
            "--etc-dir", str(self.root / "etc"),
            "--odile-lib", str(self.root / "lib"),
            "--manifest-unit", str(self.root / "manifest.service"),
            "--pin", str(self.root / "pin.json"),
            "--boot-key", str(self.root / "boot-key.pub"),
            "--boot-record", str(self.root / "boot-record.json"),
            "--host-key", str(self.root / "host-key.pub"),
            "--hostname-path", str(self.hostname),
            "--hosts-path", str(self.hosts),
            "--first-visit-state", str(self.root / "first-visit.json"),
            "--first-visit-port", "0",
        ] + list(extra)

    def test_a_first_boot_that_fails_closed_still_sets_the_hostname(self) -> None:
        # No setup token and an empty release dir: the boot stops fail-closed,
        # but the name was re-derived before any of that.
        code = self.firstboot.main(self.argv())
        self.assertEqual(code, 0)
        self.assertEqual(self.hostname.read_text(encoding="utf-8"), "odl-348b2c\n")
        self.assertIn("odl-348b2c", self.hosts.read_text(encoding="utf-8"))
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("hostname", log)
        self.assertIn("write", log)

    def test_check_is_read_only(self) -> None:
        before = (self.hostname.read_bytes(), self.hosts.read_bytes(),
                  self.hostname.stat().st_mtime_ns, self.hosts.stat().st_mtime_ns)
        with mock.patch.object(self.firstboot.socket, "sethostname") as rename:
            code = self.firstboot.main(self.argv("--check"))
        after = (self.hostname.read_bytes(), self.hosts.read_bytes(),
                 self.hostname.stat().st_mtime_ns, self.hosts.stat().st_mtime_ns)
        self.assertEqual(code, 1)  # not installed, and it says so by exit code
        self.assertEqual(before, after)
        rename.assert_not_called()

    def test_the_unit_asks_for_the_kernel_name(self) -> None:
        unit = (ROOT / "deploy" / "image" / "odile-firstboot.service").read_text(
            encoding="utf-8")
        execs = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(execs), 1, unit)
        self.assertIn("--kernel-hostname apply", execs[0])


if __name__ == "__main__":
    unittest.main()
