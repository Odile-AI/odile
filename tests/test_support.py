#!/usr/bin/python3
"""Tests for `support/odile_support.py` — the diagnostics bundle and the grant.

Hermetic: every test runs against a temporary root (`--root`), with the
system commands replaced by stubs in a temporary `PATH`. Nothing reads the
host's `/etc`, `/root` or `/var`, and no test needs a running sshd or systemd:
the forced command is exercised by running `session` with a pipe, the same way
sshd runs it.

    /usr/bin/python3 -m unittest tests.test_support -v
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "support" / "odile_support.py"

# Loads the program under test and holds its `Popen` until the test has sent
# SIGTERM, so the signal lands in the window between the start line and a live
# handler — the window a loaded `make check` was losing the end line in.
WINDOW_HARNESS = textwrap.dedent(
    """
    import importlib.util
    import sys
    import time
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("odile_support_window", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    release = Path(sys.argv[3])


    class FakeProcess:
        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass


    def fake_popen(argv, **kwargs):
        deadline = time.monotonic() + 20
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return FakeProcess()


    module.subprocess.Popen = fake_popen
    root = Path(sys.argv[2])
    args = module.build_parser().parse_args(
        ["--root", str(root), "session", "--shell", "/bin/sh"])
    raise SystemExit(module.cmd_session(args, module.Paths(root)))
    """
)

# Public halves of throwaway keys generated for these tests. The private halves
# were discarded; a public key is not a secret.
BOOT_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM9gjx8NSPgufi038PgBLMVRJqrxc5GDXTD/DRvt0iW9 "
    "odile-bench throwaway boot key"
)
SUPPORT_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICVctkmoTeaCTF66CHKXs+NzJwJI29Ed5tLJdSy4XHUm "
    "support@odile.fyi (ODL-217 pending)"
)
BOOT_LINE = BOOT_KEY + "\n"

# Built by concatenation so the repository's own secret scanner does not read a
# fixture as a real credential: the assembled values are what the diag must
# redact, and the source carries no 32-character token or PEM block.
def _planted(*parts: str) -> str:
    return "".join(parts)


PLANTED = {
    "near": "NEAR_API_KEY=" + _planted("sk-", "planted-", "near-", "key-", "0123456789abcdef"),
    "tenant_key": "HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY=" + _planted("near-key-", "planted-", "9f3c2b1a"),
    "backup": "Backup password:     " + _planted("Hunter2-", "Planted-", "Backup"),
    "signin": "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=" + _planted("scrypt$", "planted$", "hash"),
    "prompt": "PROMPT_TEXT_UNIQUE_7c1e: please rotate the flux capacitor",
    "file": "UNIQUE_FILE_CONTENT_9f3a: the answer is forty-two",
    "token": "Authorization: Bearer " + _planted("planted-", "bearer-", "token-", "abcdef012345"),
    "log_password": "password=" + _planted("hunter2-", "planted"),
    "journal": "NEAR_TOKEN=" + _planted("planted-", "journal-", "token-", "1234"),
    "private_key": (
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n"
        + _planted("b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ", "AAAAAAAAABAAAAMwAAAAtzc2gtZW") + "\n"
        + "-----END " + "OPENSSH PRIVATE KEY-----"
    ),
}


def load_module():
    spec = importlib.util.spec_from_file_location("odile_support_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ush = load_module()


def run_keygen_fingerprint(public_line: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as handle:
        handle.write(public_line + "\n")
        path = handle.name
    try:
        out = subprocess.run(["ssh-keygen", "-lf", path], capture_output=True, text=True, check=True)
        return out.stdout.split()[1]
    finally:
        os.unlink(path)


class FakeRoot:
    """A throwaway filesystem root plus command stubs on PATH."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="odile-support-test-"))
        self.root = self.dir / "rootfs"
        self.bin = self.dir / "bin"
        for path in (
            self.root / "etc/odile",
            self.root / "etc/systemd/system",
            self.root / "root/.ssh",
            self.root / "var/lib/odile",
            self.root / "var/log",
            self.root / "usr/local/sbin",
            self.root / "home/tenant/.hermes/sessions",
            self.root / "home/tenant/workspace",
            self.bin,
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "root/.ssh").chmod(0o700)
        (self.root / "etc/odile/support-key.pub").write_text(SUPPORT_KEY + "\n", encoding="utf-8")
        (self.root / "root/.ssh/authorized_keys").write_text(BOOT_LINE, encoding="utf-8")
        (self.root / "root/.ssh/authorized_keys").chmod(0o600)
        program = self.root / "usr/local/sbin/odile-support"
        program.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        program.chmod(0o755)
        self.write_stubs()

    # ---------------------------------------------------------------- fixtures

    def stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)

    def write_stubs(self) -> None:
        self.stub("uname", 'echo "Linux odile-bench 6.12.0-test #1 SMP x86_64 GNU/Linux"')
        self.stub("uptime", 'echo "up 3 days, 4 hours"')
        self.stub("nproc", "echo 4")
        self.stub("free", 'echo "               total        used        free"; echo "Mem:           4.0Gi       1.2Gi       2.8Gi"')
        self.stub("df", 'echo "Filesystem     Type  Size  Used Avail Use% Mounted on"; echo "/dev/mapper/root_crypt ext4  100G   20G   80G  20% /"')
        self.stub("du", 'echo "20G\t/var/lib/odile"; echo "1.2G\t/var/log"')
        self.stub("lsblk", 'echo "NAME          SIZE TYPE FSTYPE MOUNTPOINTS"; echo "vda           100G disk"; echo "└─root_crypt  100G crypt ext4 /"')
        self.stub("cryptsetup", 'echo "/dev/mapper/root_crypt is active and is in use."; echo "  type:    LUKS2"; echo "  cipher:  aes-xts-plain64"; echo "  keysize: 512 bits"')
        self.stub(
            "systemctl",
            'case "$1" in\n'
            '  is-active) shift; for u in "$@"; do echo "active"; done ;;\n'
            '  --failed) echo "" ;;\n'
            '  status) echo "odile-agent.service - Odile agent"; echo "     Active: active (running)" ;;\n'
            '  daemon-reload|enable|restart|start) echo "stub systemctl $*" ;;\n'
            '  *) echo "stub systemctl $*" ;;\n'
            "esac",
        )
        self.stub(
            "journalctl",
            'echo "2026-11-12T17:00:00Z odile-agent[1]: service started"\n'
            + 'echo "2026-11-12T17:00:01Z odile-agent[1]: '
            + PLANTED["journal"]
            + '"',
        )
        self.stub("dpkg-query", 'echo "python3 3.13.5-1"; echo "openssh-server 1:10.0p1-7"')
        self.stub("ss", 'echo "State  Recv-Q Send-Q Local Address:Port"; echo "LISTEN 0      128    127.0.0.1:9120"')
        self.stub("ip", 'echo "lo               UNKNOWN        127.0.0.1/8"; echo "ens18            UP             203.0.113.87/24"')
        self.stub("openssl", 'echo "OpenSSL 3.5.1 1 Jul 2025"')
        self.stub("ssh", 'echo "OpenSSH_10.0p1 Debian-7, OpenSSL 3.5.1" >&2')

    def plant_secrets(self) -> None:
        (self.root / "etc/odile/recovery.txt").write_text(
            "Odile recovery file\n" + PLANTED["backup"] + "\n", encoding="utf-8")
        (self.root / "etc/odile/key.env").write_text(PLANTED["tenant_key"] + "\n", encoding="utf-8")
        (self.root / "home/tenant/.hermes/.env").write_text(
            PLANTED["near"] + "\n" + PLANTED["signin"] + "\n", encoding="utf-8")
        (self.root / "home/tenant/.hermes/sessions/abc.jsonl").write_text(
            '{"role":"user","content":"' + PLANTED["prompt"] + '"}\n', encoding="utf-8")
        (self.root / "home/tenant/workspace/notes.txt").write_text(
            PLANTED["file"] + "\n", encoding="utf-8")
        (self.root / "var/log/odile-support.log").write_text(
            "2026-11-12T17:00:00Z event=start grant=deadbeef0001\n"
            + PLANTED["log_password"] + "\n"
            + PLANTED["private_key"] + "\n", encoding="utf-8")
        (self.root / "var/lib/odile/access-report.txt").write_text(
            "read from your machine, 2026-11-12T15:07:06Z\n"
            "verify_and_unlock — access report\n"
            "  usable keys    PASS  0 usable user keys\n"
            "  seal           PASS  policy 0x30000; seal_id 53ac131dda13e47a; "
            "measurement 9f01725afd71968f74e6dec5f7768b9dfaf52a46cbcfe8a49e106b5cff94028080a47c3d7f56dd7c5c49e6781517f137\n"
            "  operator key   PASS  none (authorized_keys: 1 line(s), the measured throwaway)\n",
            encoding="utf-8")
        (self.root / "etc/odile/boot-record.json").write_text(json.dumps({
            "release": "2026.11.2",
            "measurement": "9f01725afd71968f74e6dec5f7768b9dfaf52a46cbcfe8a49e106b5cff94028080a47c3d7f56dd7c5c49e6781517f137",
            "cmdline_sha256": "e21d10d8",
            "installer_sha256": "4aee80c5",
        }) + "\n", encoding="utf-8")
        (self.root / "etc/odile/seed.json").write_text(json.dumps({
            "slug": "odl-test01",
            "hermes_version": "2026.11.1",
            "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/odl-test01",
            "near_api_key": "sk-seed-planted-near-key",
        }) + "\n", encoding="utf-8")

    def enable_systemd(self) -> None:
        (self.root / "run/systemd/system").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------- runs

    def env(self, now: str | None = None) -> dict:
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env.pop("ODILE_SUPPORT_NOW", None)
        if now:
            env["ODILE_SUPPORT_NOW"] = now
        return env

    def run(self, *args: str, now: str | None = None, stdin: str | None = None,
            script: Path = SCRIPT) -> subprocess.CompletedProcess:
        argv = [sys.executable, str(script), "--root", str(self.root), *args]
        return subprocess.run(argv, capture_output=True, text=True, input=stdin,
                              env=self.env(now), timeout=60)

    def diag(self, *args: str, now: str | None = None) -> subprocess.CompletedProcess:
        argv = [sys.executable, str(SCRIPT), "--root", str(self.root), "diag", *args]
        return subprocess.run(argv, capture_output=True, text=True, env=self.env(now), timeout=60)

    # ------------------------------------------------------------------ reads

    def authorized(self) -> str:
        return (self.root / "root/.ssh/authorized_keys").read_text(encoding="utf-8")

    def state_path(self) -> Path:
        return self.root / "var/lib/odile/support-grant.json"

    def state(self) -> dict:
        return json.loads(self.state_path().read_text(encoding="utf-8"))

    def support_lines(self) -> list[str]:
        return [line for line in self.authorized().splitlines() if "odile-support-grant-" in line]

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class SupportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeRoot()
        self.addCleanup(self.fake.cleanup)

    def assert_no_support_line(self) -> None:
        self.assertEqual([line for line in self.fake.authorized().splitlines()
                          if "odile-support" in line], [])

    def wait_for_log(self, needle: str, timeout: float = 10.0) -> str:
        """Return the log text once `needle` appears, or fail with what it held."""
        log = self.fake.root / "var/log/odile-support.log"
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            text = log.read_text(encoding="utf-8") if log.exists() else ""
            if needle in text:
                return text
            time.sleep(0.05)
        self.fail(f"{needle} never reached {log}: {log.read_text() if log.exists() else ''}")


class StatusTests(SupportTestCase):
    def test_the_default_state_is_no_grant(self) -> None:
        out = self.fake.run("status")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines()[0], "no grant")
        self.assertFalse(self.fake.state_path().exists())
        self.assertEqual(self.fake.authorized(), BOOT_LINE)

    def test_status_json_reports_a_null_grant(self) -> None:
        out = self.fake.run("status", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertIsNone(data["grant"])
        self.assertIn("authorized_keys", data)

    def test_status_does_not_create_the_state_or_log(self) -> None:
        self.fake.run("status")
        self.assertFalse(self.fake.state_path().exists())
        self.assertFalse((self.fake.root / "var/log/odile-support.log").exists())


class GrantTests(SupportTestCase):
    now = "2026-11-12T17:00:00Z"

    def test_grant_installs_one_forced_command_line_and_keeps_the_boot_key(self) -> None:
        out = self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = self.fake.authorized().splitlines()
        self.assertEqual(len(lines), 2, self.fake.authorized())
        self.assertEqual(lines[0], BOOT_KEY)
        grant = lines[1]
        forced = (f'command="{self.fake.root}/usr/local/sbin/odile-support '
                  f'--root {self.fake.root} session"')
        self.assertIn(forced, grant)
        self.assertIn("no-agent-forwarding", grant)
        self.assertIn("no-port-forwarding", grant)
        self.assertIn("no-X11-forwarding", grant)
        self.assertIn("no-user-rc", grant)
        self.assertNotIn("no-pty", grant.replace("no-port-forwarding", ""))
        self.assertIn(SUPPORT_KEY.split()[1], grant)
        self.assertRegex(grant, r"odile-support-grant-[0-9a-f]{12}$")
        state = self.fake.state()
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["expires_at"], "2026-11-13T17:00:00Z")
        self.assertEqual(state["created_at"], self.now)
        self.assertEqual(state["key_fingerprint"],
                         run_keygen_fingerprint(SUPPORT_KEY))
        self.assertNotIn("PRIVATE", json.dumps(state))
        # Nothing a reboot clears: the grant lives in /root and /var, not /run.
        self.assertEqual([p for p in (self.fake.root / "run").rglob("*")], [])

    def test_grant_state_is_public_material_only(self) -> None:
        self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertEqual(self.fake.state_path().stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.fake.root / "root/.ssh/authorized_keys").stat().st_mode & 0o777, 0o600)

    def test_grant_prints_the_card_the_expiry_and_the_revoke_command(self) -> None:
        out = self.fake.run("grant", "--hours", "24", now=self.now)
        text = out.stdout
        folded = " ".join(text.split())
        self.assertIn("grant live", text)
        self.assertIn("2026-11-13T17:00:00Z", text)
        self.assertIn("sudo odile-support revoke", text)
        for sentence in (
            "handover, not a peek",
            "mounted disk",
            "agent's memory and past prompts",
            "sign-in hash",
            "backup password if the recovery file is still on the machine",
            "add a LUKS keyslot",
            "copy the volume key",
            "Expiry and revoke block new logins",
            "an open session keeps running until it exits",
            "cryptsetup luksDump",
            "cryptsetup status root_crypt",
            "sed -n 's/^ *device: *//p'",
            "if command -v crontab >/dev/null; then sudo crontab -l; else echo 'crontab not installed'; fi",
            "systemctl list-timers --all",
            "authorized_keys",
            "A reboot does not close the grant",
            "root shell can edit it",
        ):
            self.assertIn(sentence, folded, f"the grant card is missing: {sentence}")
        self.assertIn("w", text)
        self.assertIn("pkill -t pts/N", text)

    def test_the_grant_card_names_no_fixed_disk_device(self) -> None:
        # The tenant's LUKS is /dev/sda3 (the mapper is root_crypt); /dev/vda2
        # was a guess that `cryptsetup luksDump` refuses on the image.
        out = self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertNotIn("/dev/vda", out.stdout)
        self.assertNotIn("/dev/sda", out.stdout)
        self.assertIn('DEV=$(sudo cryptsetup status root_crypt', " ".join(out.stdout.split()))

    def test_grant_is_idempotent(self) -> None:
        first = self.fake.run("grant", "--hours", "24", now=self.now)
        second = self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.fake.support_lines()), 1)
        self.assertEqual(len(self.fake.authorized().splitlines()), 2)
        self.assertIn("refreshed", second.stdout)

    def test_grant_dry_run_changes_nothing(self) -> None:
        out = self.fake.run("grant", "--hours", "24", "--dry-run", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("would", out.stdout.lower())
        self.assertEqual(self.fake.authorized(), BOOT_LINE)
        self.assertFalse(self.fake.state_path().exists())

    def test_grant_rejects_an_hour_count_the_operator_did_not_mean(self) -> None:
        for hours in ("0", "-1", "721", "abc"):
            out = self.fake.run("grant", "--hours", hours, now=self.now)
            self.assertNotEqual(out.returncode, 0, hours)
            self.assertEqual(self.fake.authorized(), BOOT_LINE, hours)

    def test_grant_refuses_a_missing_or_invalid_support_key(self) -> None:
        key_file = self.fake.root / "etc/odile/support-key.pub"
        cases = {
            "missing": None,
            "empty": "\n",
            "private": "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n",
            "two keys": SUPPORT_KEY + "\n" + BOOT_KEY + "\n",
            "options prefix": 'command="x" ' + SUPPORT_KEY + "\n",
            "bad base64": "ssh-ed25519 !!!notbase64!!! comment\n",
        }
        for label, content in cases.items():
            with self.subTest(label):
                if content is None:
                    key_file.unlink(missing_ok=True)
                else:
                    key_file.write_text(content, encoding="utf-8")
                out = self.fake.run("grant", "--hours", "24", now=self.now)
                self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
                self.assertIn("support public key", out.stderr)
                self.assertEqual(self.fake.authorized(), BOOT_LINE)
                self.assertFalse(self.fake.state_path().exists())

    def test_grant_refuses_a_key_comment_with_a_newline(self) -> None:
        (self.fake.root / "etc/odile/support-key.pub").write_text(
            SUPPORT_KEY + "\nssh-ed25519 AAAA injected\n", encoding="utf-8")
        out = self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.fake.authorized(), BOOT_LINE)

    def test_grant_refuses_when_the_installed_program_is_missing(self) -> None:
        (self.fake.root / "usr/local/sbin/odile-support").unlink()
        out = self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertEqual(out.returncode, 3)
        self.assertIn("/usr/local/sbin/odile-support", out.stderr)
        self.assertEqual(self.fake.authorized(), BOOT_LINE)

    def test_grant_installs_the_expiry_timer_and_enables_it(self) -> None:
        self.fake.enable_systemd()
        out = self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        service = (self.fake.root / "etc/systemd/system/odile-support-expire.service").read_text()
        timer = (self.fake.root / "etc/systemd/system/odile-support-expire.timer").read_text()
        self.assertIn(
            f"ExecStart={self.fake.root}/usr/local/sbin/odile-support "
            f"--root {self.fake.root} expire --quiet", service)
        self.assertIn("OnUnitActiveSec=30s", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("systemctl enable --now odile-support-expire.timer: ok", out.stdout)

    def test_grant_dry_run_with_systemd_writes_no_units_and_no_state(self) -> None:
        self.fake.enable_systemd()
        out = self.fake.run("grant", "--hours", "24", "--dry-run", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("would write and enable", out.stdout)
        self.assertIn("(0644)", out.stdout)
        self.assertFalse((self.fake.root / "etc/systemd/system/odile-support-expire.timer").exists())
        self.assertFalse((self.fake.root / "etc/systemd/system/odile-support-expire.service").exists())
        self.assertFalse(self.fake.state_path().exists())
        self.assertEqual(self.fake.authorized(), BOOT_LINE)

    def test_grant_rolls_back_when_the_expiry_timer_cannot_be_enabled(self) -> None:
        self.fake.enable_systemd()
        self.fake.stub(
            "systemctl",
            'case "$1" in\\n'
            '  daemon-reload) echo ok ;;\\n'
            '  enable) echo "systemctl: unit enable failed" >&2; exit 1 ;;\\n'
            '  *) echo ok ;;\\n'
            "esac",
        )
        out = self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertEqual(out.returncode, 5, out.stdout + out.stderr)
        self.assertIn("rolled back", out.stderr)
        self.assertEqual(self.fake.authorized(), BOOT_LINE)
        self.assertFalse(self.fake.state_path().exists())

    def test_status_json_names_a_live_grant_and_its_installed_line(self) -> None:
        out = self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        status = self.fake.run("status", "--json", now=self.now)
        data = json.loads(status.stdout)
        self.assertEqual(data["grant"]["grant_id"], self.fake.state()["grant_id"])
        self.assertTrue(data["grant"]["key_installed"])
        self.assertEqual(data["authorized_keys"]["grant_lines"], 1)

    def test_grant_without_systemd_says_expiry_is_enforced_on_the_next_run(self) -> None:
        out = self.fake.run("grant", "--hours", "24", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("systemd is not running", out.stdout)
        self.assertFalse((self.fake.root / "etc/systemd/system/odile-support-expire.timer").exists())


class RevokeTests(SupportTestCase):
    now = "2026-11-12T17:00:00Z"

    def test_revoke_removes_only_the_grant_line_and_clears_the_state(self) -> None:
        self.fake.run("grant", "--hours", "24", now=self.now)
        out = self.fake.run("revoke", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.fake.authorized(), BOOT_LINE)
        self.assertEqual(self.fake.state()["revoked_at"], self.now)
        self.assertIn("revoked", out.stdout)

    def test_revoke_is_idempotent_when_no_grant_exists(self) -> None:
        out = self.fake.run("revoke", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "no grant")
        self.assertEqual(self.fake.authorized(), BOOT_LINE)

    def test_revoke_removes_the_line_even_when_the_state_file_is_gone(self) -> None:
        self.fake.run("grant", "--hours", "24", now=self.now)
        self.fake.state_path().unlink()
        out = self.fake.run("revoke", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.fake.authorized(), BOOT_LINE)

    def test_revoke_leaves_a_decoy_line_that_merely_mentions_the_name(self) -> None:
        decoy = BOOT_KEY + " odile-support-grant-decoy"
        self.fake.run("grant", "--hours", "24", now=self.now)
        (self.fake.root / "root/.ssh/authorized_keys").write_text(
            self.fake.authorized() + decoy + "\n", encoding="utf-8")
        self.fake.run("revoke", now=self.now)
        self.assertIn(decoy, self.fake.authorized())
        self.assertEqual(len(self.fake.support_lines()), 1)

    def test_revoke_prints_the_open_session_commands(self) -> None:
        self.fake.run("grant", "--hours", "24", now=self.now)
        out = self.fake.run("revoke", now=self.now)
        self.assertIn("w", out.stdout)
        self.assertIn("pkill -t pts/N", out.stdout)
        self.assertIn("open session", out.stdout)

    def test_revoke_prints_the_post_revoke_checks_and_they_name_no_fixed_device(self) -> None:
        self.fake.run("grant", "--hours", "24", now=self.now)
        out = self.fake.run("revoke", now=self.now)
        folded = " ".join(out.stdout.split())
        for sentence in (
            "sudo cat /root/.ssh/authorized_keys",
            "DEV=$(sudo cryptsetup status root_crypt | sed -n 's/^ *device: *//p')",
            'sudo cryptsetup luksDump "$DEV"',
            "if command -v crontab >/dev/null; then sudo crontab -l; else echo 'crontab not installed'; fi",
            "sudo systemctl list-timers --all",
        ):
            self.assertIn(sentence, folded, f"the revoke output is missing: {sentence}")
        self.assertNotIn("/dev/vda", out.stdout)
        self.assertNotIn("/dev/sda", out.stdout)

    def test_revoke_dry_run_changes_nothing(self) -> None:
        self.fake.run("grant", "--hours", "24", now=self.now)
        before = self.fake.authorized()
        out = self.fake.run("revoke", "--dry-run", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.fake.authorized(), before)
        self.assertTrue(self.fake.state_path().exists())

    def test_status_after_revoke_says_no_grant_and_names_the_time(self) -> None:
        self.fake.run("grant", "--hours", "24", now=self.now)
        self.fake.run("revoke", now=self.now)
        out = self.fake.run("status", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), f"no grant (revoked {self.now})")
        self.assert_no_support_line()


class ExpireTests(SupportTestCase):
    now = "2026-11-12T17:00:00Z"

    def grant(self, hours: str = "1", now: str = now) -> None:
        out = self.fake.run("grant", "--hours", hours, now=now)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_expire_removes_a_due_key_and_says_why(self) -> None:
        self.grant()
        later = "2026-11-12T18:00:01Z"
        out = self.fake.run("expire", now=later)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assert_no_support_line()
        self.assertEqual(self.fake.state()["expired_at"], "2026-11-12T18:00:01Z")
        status = self.fake.run("status", now=later)
        self.assertEqual(status.stdout.strip().splitlines()[0],
                         "no grant (expired 2026-11-12T18:00:00Z)")

    def test_expire_keeps_a_live_grant(self) -> None:
        self.grant()
        out = self.fake.run("expire", now="2026-11-12T17:59:59Z")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.fake.support_lines()), 1)
        self.assertTrue(self.fake.state_path().exists())

    def test_status_expires_a_due_grant_on_its_own(self) -> None:
        self.grant()
        out = self.fake.run("status", now="2026-11-12T18:30:00Z")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("no grant (expired"))
        self.assert_no_support_line()

    def test_expire_without_a_grant_is_quiet_and_succeeds(self) -> None:
        out = self.fake.run("expire", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "")


class ConvergeTests(SupportTestCase):
    """A reboot does not close a grant — measured against what a boot really does.

    The tenant image runs the release installer on every boot
    (`odile-release-setup.service`), and its `ensure_verify_surface` writes
    exactly one line — the measured boot key — to root's `authorized_keys`. A
    grant line written before the reboot is therefore *deleted* by the boot, not
    kept. The grant's own timer must put it back; the state file and the expiry
    are what make the grant live, not the bytes in `authorized_keys`.
    """

    now = "2026-11-12T17:00:00Z"
    after_boot = "2026-11-12T17:00:40Z"

    def grant(self, *extra: str) -> str:
        out = self.fake.run("grant", "--hours", "1", *extra, now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        return self.fake.support_lines()[0]

    def boot_installer_rewrite(self) -> None:
        """Exactly what `ensure_verify_surface` does: one line, the boot key."""
        path = self.fake.root / "root/.ssh/authorized_keys"
        path.write_text(BOOT_LINE, encoding="utf-8")
        path.chmod(0o600)

    def test_expire_restores_a_live_grant_the_boot_installer_evicted(self) -> None:
        line = self.grant()
        self.boot_installer_rewrite()
        out = self.fake.run("expire", now=self.after_boot)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.fake.support_lines(), [line])
        self.assertEqual(len(self.fake.authorized().splitlines()), 2)
        self.assertEqual((self.fake.root / "root/.ssh/authorized_keys").stat().st_mode & 0o777, 0o600)
        log = (self.fake.root / "var/log/odile-support.log").read_text(encoding="utf-8")
        self.assertIn("event=reassert", log)
        self.assertIn(f"grant={self.fake.state()['grant_id']}", log)

    def test_the_restored_line_is_byte_identical_to_the_granted_one(self) -> None:
        line = self.grant()
        self.boot_installer_rewrite()
        self.fake.run("expire", now=self.after_boot)
        self.assertEqual(self.fake.support_lines()[0], line)

    def test_status_restores_the_line_and_says_so(self) -> None:
        self.grant()
        self.boot_installer_rewrite()
        out = self.fake.run("status", now=self.after_boot)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("grant live", out.stdout)
        self.assertIn("key line:   present", out.stdout)
        self.assertIn("restored:", out.stdout)
        self.assertIn("rewrote", out.stdout)
        data = json.loads(self.fake.run("status", "--json", now=self.after_boot).stdout)
        self.assertTrue(data["grant"]["key_installed"])
        self.assertEqual(data["grant"]["restored_at"], self.after_boot)

    def test_expire_does_not_resurrect_a_revoked_grant(self) -> None:
        self.grant()
        self.fake.run("revoke", now=self.now)
        self.fake.run("expire", now=self.after_boot)
        self.assert_no_support_line()
        log = (self.fake.root / "var/log/odile-support.log").read_text(encoding="utf-8")
        self.assertNotIn("event=reassert", log)

    def test_expire_does_not_resurrect_an_expired_grant(self) -> None:
        self.grant()
        self.fake.run("expire", now="2026-11-12T18:00:01Z")
        self.fake.run("expire", now="2026-11-12T18:00:31Z")
        self.assert_no_support_line()
        log = (self.fake.root / "var/log/odile-support.log").read_text(encoding="utf-8")
        self.assertNotIn("event=reassert", log)

    def test_a_grant_without_the_timer_converges_on_the_next_root_run(self) -> None:
        self.grant("--no-systemd")
        self.boot_installer_rewrite()
        out = self.fake.run("expire", now=self.after_boot)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.fake.support_lines()), 1)

    def test_a_corrupt_state_key_is_never_installed(self) -> None:
        self.grant()
        self.boot_installer_rewrite()
        state = self.fake.state()
        state["key_blob"] = "NOT-A-KEY"
        self.fake.state_path().write_text(json.dumps(state), encoding="utf-8")
        out = self.fake.run("expire", now=self.after_boot)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.fake.authorized(), BOOT_LINE)
        log = (self.fake.root / "var/log/odile-support.log").read_text(encoding="utf-8")
        self.assertIn("event=reassert_failed", log)
        self.assertNotIn("event=reassert ", log)

    def test_a_reboot_then_the_timer_leaves_a_working_grant_and_revoke_closes_it(self) -> None:
        self.grant()
        self.boot_installer_rewrite()
        self.fake.run("expire", now=self.after_boot)
        out = self.fake.run("session", "--shell", "/bin/sh", now=self.after_boot,
                            stdin="echo AFTER_REBOOT\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("AFTER_REBOOT", out.stdout)
        revoke = self.fake.run("revoke", now="2026-11-12T17:05:00Z")
        self.assertEqual(revoke.returncode, 0, revoke.stderr)
        refused = self.fake.run("session", "--shell", "/bin/sh",
                                now="2026-11-12T17:05:01Z", stdin="echo NOPE\n")
        self.assertEqual(refused.returncode, 1)
        self.assertNotIn("NOPE", refused.stdout)


class ReasonTests(SupportTestCase):
    now = "2026-11-12T17:00:00Z"

    def test_grant_records_the_reason_on_the_card_in_the_state_and_in_the_log(self) -> None:
        out = self.fake.run("grant", "--hours", "2", "--reason", "disk full after an update",
                            now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("disk full after an update", out.stdout)
        self.assertEqual(self.fake.state()["reason"], "disk full after an update")
        log = (self.fake.root / "var/log/odile-support.log").read_text(encoding="utf-8")
        self.assertIn("reason='disk full after an update'", log)

    def test_the_reason_defaults_to_a_dash(self) -> None:
        self.fake.run("grant", "--hours", "2", now=self.now)
        self.assertEqual(self.fake.state()["reason"], "-")
        data = json.loads(self.fake.run("status", "--json", now=self.now).stdout)
        self.assertEqual(data["grant"]["reason"], "-")

    def test_a_reason_with_a_newline_is_one_line_on_the_card(self) -> None:
        out = self.fake.run("grant", "--hours", "2", "--reason",
                            "first line\nsecond line\twith a tab", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.fake.state()["reason"], "first line second line with a tab")
        card = out.stdout.split("  expires:", 1)[0]
        reason_lines = [line for line in card.splitlines() if line.strip().startswith("reason:")]
        self.assertEqual(len(reason_lines), 1, card)
        self.assertIn("first line second line with a tab", reason_lines[0])


class SessionTests(SupportTestCase):
    now = "2026-11-12T17:00:00Z"

    def grant(self, hours: str = "1") -> None:
        out = self.fake.run("grant", "--hours", hours, now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_session_refuses_without_a_grant(self) -> None:
        out = self.fake.run("session", "--shell", "/bin/sh", now=self.now, stdin="echo LIVE\n")
        self.assertEqual(out.returncode, 1)
        self.assertIn("no live grant", out.stderr)
        self.assertNotIn("LIVE", out.stdout)

    def test_session_refuses_after_expiry_and_removes_the_key(self) -> None:
        self.grant()
        out = self.fake.run("session", "--shell", "/bin/sh", now="2026-11-12T18:00:01Z", stdin="echo LIVE\n")
        self.assertEqual(out.returncode, 1)
        self.assertIn("expired", out.stderr)
        self.assert_no_support_line()

    def test_session_logs_start_and_end_and_runs_the_granted_shell(self) -> None:
        self.grant()
        env_out = self.fake.run("session", "--shell", "/bin/sh", now=self.now, stdin="echo LIVE\n")
        self.assertEqual(env_out.returncode, 0, env_out.stderr)
        self.assertIn("LIVE", env_out.stdout)
        log = (self.fake.root / "var/log/odile-support.log").read_text(encoding="utf-8")
        self.assertIn("event=start", log)
        self.assertIn("event=end", log)
        self.assertIn("event=end", log)
        self.assertRegex(log, r"event=end grant=[0-9a-f]{12} rc=0")
        self.assertEqual((self.fake.root / "var/log/odile-support.log").stat().st_mode & 0o777, 0o644)

    def test_status_shows_the_last_session(self) -> None:
        self.grant()
        self.fake.run("session", "--shell", "/bin/sh", now=self.now, stdin="echo LIVE\n")
        out = self.fake.run("status", now="2026-11-12T17:00:05Z")
        self.assertIn("last session", out.stdout)
        self.assertIn("event=start", out.stdout.replace("start ", "event=start "))

    def test_session_logs_an_end_line_when_the_open_session_is_killed(self) -> None:
        self.grant()
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--root", str(self.fake.root), "session",
             "--shell", "/bin/sh"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.fake.env(now=self.now), text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())

        self.wait_for_log("event=start")
        process.terminate()
        process.communicate(timeout=10)
        text = self.wait_for_log("event=end")
        self.assertIn("rc=143", text)
        self.assertIn("signal=SIGTERM", text)

    def test_killing_the_wrapper_the_moment_the_start_line_appears_never_leaves_it_open(self) -> None:
        """Kill on the visible start line, 25 times: every start still gets an end.

        This is the production race, not a window a harness holds open. Each
        iteration polls the log tightly, sends SIGTERM the instant this
        iteration's `event=start` is readable, and only then waits. Pre-fix the
        wrapper has no handler at that point, dies with the default disposition
        and leaves a start line with no end line; 25 back-to-back kills make
        that gap wide enough to see without a sleep in the test.
        """
        self.grant()
        log = self.fake.root / "var/log/odile-support.log"
        kills = 25

        def text() -> str:
            return log.read_text(encoding="utf-8") if log.exists() else ""

        for attempt in range(1, kills + 1):
            starts = text().count("event=start")
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), "--root", str(self.fake.root), "session",
                 "--shell", "/bin/sh"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env=self.fake.env(now=self.now), text=True)
            try:
                deadline = time.monotonic() + 10
                while text().count("event=start") == starts:
                    if time.monotonic() > deadline:
                        self.fail(f"attempt {attempt}: no start line: {text()}")
                    time.sleep(0.001)
                process.terminate()          # the start line is visible; signal, no sleep
                _, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            deadline = time.monotonic() + 10
            while text().count("event=end") < attempt and time.monotonic() < deadline:
                time.sleep(0.001)
            log_text = text()
            self.assertEqual(log_text.count("event=start"), attempt, f"attempt {attempt}: {log_text}")
            self.assertEqual(log_text.count("event=end"), attempt,
                             f"attempt {attempt} left a start with no end: {log_text}")
            last_end = [line for line in log_text.splitlines() if "event=end" in line][-1]
            self.assertRegex(last_end, r"event=end .* rc=143 .* signal=SIGTERM")
            self.assertEqual(process.returncode, 143, f"attempt {attempt}: {stderr}\n{log_text}")

    def test_a_sigterm_before_the_shell_exists_still_writes_the_end_line(self) -> None:
        """The signal lands after the start line and before a shell could exist.

        The harness holds `Popen` until the test has sent SIGTERM, so the signal
        is delivered exactly in the window the loaded gate was losing the end
        line in: the start line is on disk, the shell has not been started, and
        no handler is live yet. The wrapper must still write `event=end` with
        `signal=SIGTERM` and `rc=143`.
        """
        self.grant()
        log = self.fake.root / "var/log/odile-support.log"
        release = self.fake.dir / "let-popen-return"
        harness = self.fake.dir / "window_harness.py"
        harness.write_text(WINDOW_HARNESS, encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(harness), str(SCRIPT), str(self.fake.root), str(release)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.fake.env(now=self.now), text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())

        self.wait_for_log("event=start")
        process.terminate()          # inside Popen: no shell exists yet
        release.write_text("go\n", encoding="utf-8")
        process.wait(timeout=20)
        stdout, stderr = process.communicate()
        text = log.read_text(encoding="utf-8")
        self.assertEqual(process.returncode, 143, f"{stdout}{stderr}\n{text}")
        self.assertRegex(text, r"(?m)^.*event=end .* rc=143 .*signal=SIGTERM")
        self.assertEqual(text.count("event=start"), 1, text)
        self.assertEqual(text.count("event=end"), 1, text)

    def test_the_shell_does_not_inherit_the_blocked_signals(self) -> None:
        """A blocked mask survives exec, so the wrapper must restore it.

        If the shell starts with SIGTERM blocked, `pkill -t pts/N` — the way the
        docs end a session — stops working. The probe prints its own `SigBlk`.
        """
        self.grant()
        probe = self.fake.dir / "mask-probe"
        probe.write_text(
            f"#!{sys.executable}\n"
            "for line in open('/proc/self/status'):\n"
            "    if line.startswith('SigBlk:'):\n"
            "        print(line.split()[1])\n",
            encoding="utf-8")
        probe.chmod(0o755)
        out = self.fake.run("session", "--shell", str(probe), now=self.now)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        mask = int(out.stdout.strip().splitlines()[0], 16)
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            self.assertFalse(mask & (1 << (signum - 1)),
                             f"{signal.Signals(signum).name} is blocked in the shell: {mask:#x}")


class DiagTests(SupportTestCase):
    now = "2026-11-12T17:00:00Z"

    def test_diag_writes_the_five_sections_and_is_world_readable(self) -> None:
        self.fake.plant_secrets()
        out = self.fake.diag(now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        match = re.search(r"wrote (\S+)", out.stdout)
        self.assertIsNotNone(match, out.stdout)
        path = Path(match.group(1))
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        text = path.read_text()
        for section in ("system", "disk", "services", "logs", "audit", "support"):
            self.assertIn(f"===== {section} =====", text)
        self.assertIn("generated_at: " + self.now, text)
        self.assertIn("measurement 9f01725a", text)
        self.assertIn("operator key   PASS", text)

    def test_diag_contains_no_planted_secret_prompt_or_file_contents(self) -> None:
        self.fake.plant_secrets()
        out = self.fake.diag("--out", str(self.fake.dir / "diag.txt"), now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        text = Path(str(self.fake.dir / "diag.txt")).read_text()
        for label, secret in PLANTED.items():
            self.assertNotIn(secret.splitlines()[0], text, f"diag leaked {label}")
        self.assertNotIn("sk-planted-near-key", text)
        self.assertNotIn("sk-seed-planted-near-key", text)
        self.assertNotIn("near-key-planted-9f3c2b1a", text)
        self.assertNotIn("Hunter2-Planted-Backup", text)
        self.assertNotIn("scrypt$planted$hash", text)
        self.assertNotIn("PROMPT_TEXT_UNIQUE_7c1e", text)
        self.assertNotIn("UNIQUE_FILE_CONTENT_9f3a", text)
        self.assertNotIn("planted-bearer-token", text)
        self.assertNotIn("hunter2-planted", text)
        self.assertNotIn("planted-journal-token", text)
        self.assertNotIn("b3BlbnNzaC1rZXktdjE", text)

    def test_diag_says_what_it_excludes(self) -> None:
        out = self.fake.diag("--out", str(self.fake.dir / "diag.txt"), now=self.now)
        text = Path(str(self.fake.dir / "diag.txt")).read_text()
        for noun in ("passphrase", "token", "private key", "prompt history", "file contents"):
            self.assertIn(noun, text.lower())

    def test_diag_keeps_the_public_audit_numbers(self) -> None:
        self.fake.plant_secrets()
        out = self.fake.diag("--out", str(self.fake.dir / "diag.txt"), now=self.now)
        text = Path(str(self.fake.dir / "diag.txt")).read_text()
        self.assertIn("9f01725afd71968f74e6dec5f7768b9dfaf52a46cbcfe8a49e106b5cff94028080a47c3d7f56dd7c5c49e6781517f137", text)
        self.assertIn("release: 2026.11.2", text)
        self.assertIn("odl-test01", text)
        self.assertIn("2026.11.1", text)

    def test_diag_redacts_a_private_key_block_and_assignments_even_in_our_own_log(self) -> None:
        self.fake.plant_secrets()
        out = self.fake.diag("--out", str(self.fake.dir / "diag.txt"), now=self.now)
        text = Path(str(self.fake.dir / "diag.txt")).read_text()
        self.assertIn("[redacted private key]", text)
        self.assertIn("[redacted]", text)
        self.assertIn("event=start grant=deadbeef0001", text)

    def test_diag_can_write_to_stdout(self) -> None:
        out = self.fake.diag("--out", "-", now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("===== system =====", out.stdout)
        self.assertIn("===== audit =====", out.stdout)

    def test_diag_refuses_a_missing_access_report_but_still_writes(self) -> None:
        (self.fake.root / "var/lib/odile/access-report.txt").unlink(missing_ok=True)
        out = self.fake.diag("--out", str(self.fake.dir / "diag.txt"), now=self.now)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("audit report not present", Path(str(self.fake.dir / "diag.txt")).read_text())


class KeyAndRedactionUnitTests(unittest.TestCase):
    def test_fingerprint_matches_ssh_keygen(self) -> None:
        expected = run_keygen_fingerprint(SUPPORT_KEY)
        self.assertEqual(ush.fingerprint_from_line(SUPPORT_KEY), expected)

    def test_fingerprint_of_the_boot_key_matches_ssh_keygen(self) -> None:
        expected = run_keygen_fingerprint(BOOT_KEY)
        self.assertEqual(ush.fingerprint_from_line(BOOT_KEY), expected)

    def test_parse_rejects_a_private_key(self) -> None:
        with self.assertRaises(ush.KeyFileError):
            ush.parse_public_key(PLANTED["private_key"])

    def test_parse_accepts_the_seed_key_with_spaces_and_parentheses(self) -> None:
        key = ush.parse_public_key(SUPPORT_KEY)
        self.assertEqual(key.key_type, "ssh-ed25519")
        self.assertEqual(key.comment, "support@odile.fyi (ODL-217 pending)")

    def test_redaction_keeps_a_public_fingerprint(self) -> None:
        line = "boot key PASS ssh-ed25519 SHA256:I2VaMwa+RRbbA6/f4AJhQUJv3WhAnjQvNxqmWZAVPCM"
        self.assertIn("SHA256:I2VaMwa+", ush.redact(line))

    def test_the_forced_command_on_a_tenant_is_the_installed_path(self) -> None:
        state = {"grant_id": "0123456789ab", "key_type": "ssh-ed25519",
                 "key_blob": SUPPORT_KEY.split()[1]}
        line = ush.grant_line(ush.Paths(Path("/")), state)
        self.assertTrue(line.startswith(
            'command="/usr/local/sbin/odile-support session",no-agent-forwarding,'),
            line)
        self.assertNotIn("--root", line)

    def test_a_drill_root_is_carried_in_the_forced_command(self) -> None:
        paths = ush.Paths(Path("/var/tmp/odile drill"))
        self.assertEqual(
            ush.session_command(paths),
            "'/var/tmp/odile drill/usr/local/sbin/odile-support' "
            "--root '/var/tmp/odile drill' session")

    def test_redaction_handles_json_style_secrets(self) -> None:
        line = '{"near_api_key": "sk-abc123", "password": "hunter2", "hermes_version": "2026.11.1"}'
        redacted = ush.redact(line)
        self.assertNotIn("sk-abc123", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertIn("2026.11.1", redacted)


class EntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeRoot()
        self.addCleanup(self.fake.cleanup)

    def test_diag_invoked_through_its_own_name(self) -> None:
        link = self.fake.bin / "odile-diag"
        link.symlink_to(SCRIPT)
        out = subprocess.run([sys.executable, str(link), "--root", str(self.fake.root),
                              "--out", str(self.fake.dir / "diag.txt")],
                             capture_output=True, text=True, env=self.fake.env(), timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(Path(str(self.fake.dir / "diag.txt")).is_file())

    def test_support_invoked_through_its_own_name(self) -> None:
        link = self.fake.bin / "odile-support"
        link.symlink_to(SCRIPT)
        out = subprocess.run([sys.executable, str(link), "--root", str(self.fake.root), "status"],
                             capture_output=True, text=True, env=self.fake.env(), timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "no grant")


if __name__ == "__main__":
    unittest.main()
