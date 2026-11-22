"""Tests for the ODL-339 test bench deployer.

The bench is a second environment on the same box: its own release tree,
database, seed config, two units on 8082/8083 and a ``test.odile.fyi`` Caddy
block. These tests hold the four properties that make it safe to run on a live
host next to production:

* the environment file is the bench's own values — never 8080/8081 and never
  /var/lib/odile;
* both units render from a template into one release root, and the seed unit
  loads the bench config and the shared seed key through LoadCredential;
* splicing the Caddy block is idempotent (the second splice is byte-identical)
  and leaves the production site exactly as it was;
* a first apply installs, a second apply prints ``nothing to do`` and runs no
  mutating command, and one rollback returns the previous release.

The filesystem tests run the real ``main()`` against a throwaway tree with the
module paths rebound and ``run`` replaced by a recorder, so they never touch
the live host, systemd, Caddy or Cloudflare.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "bench" / "deploy_bench.py"
SITE_TEMPLATE = ROOT / "deploy" / "bench" / "odile-bench.service.in"
SEED_TEMPLATE = ROOT / "deploy" / "bench" / "odile-bench-seed.service.in"
PROBE_TEMPLATE = ROOT / "deploy" / "bench" / "odile-bench-probe.service.in"
TIMER_TEMPLATE = ROOT / "deploy" / "bench" / "odile-bench-probe.timer.in"
BLOCK = ROOT / "deploy" / "bench" / "bench.caddy"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load("odile_deploy_bench", SCRIPT)

# A live-shaped Caddyfile: the global `admin off`, the production site with
# its own seed region and catch-all, and a second site block with its own
# `handle {`. The bench block must not touch any of it.
PRODUCTION_CADDY = """\
{
\tadmin off
}

odile.fyi {
\tencode zstd gzip

\theader {
\t\tServer odile
\t}

\tlog {
\t\toutput discard
\t}

\t# BEGIN odile-seed
\t@seed path /seed
\thandle @seed {
\t\treverse_proxy 127.0.0.1:8081
\t}

\thandle {
\t\treverse_proxy 127.0.0.1:8080
\t}
\t# END odile-seed
}

gw.odile.fyi {
\thandle {
\t\treverse_proxy 127.0.0.1:9119
\t}
}
"""

PRODUCTION_CONFIG = {
    "near_api_key": "near-fixture-key",
    "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
    "support_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFIXTURE support@odile.fyi",
    "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/{slug}",
    "hermes_version": "2026.11.1",
    "mail_relay": {"host": "smtp.mail.odile.fyi", "port": 587,
                   "username": "{slug}@mail.odile.fyi", "secret": "relay-fixture",
                   "tls_ca": ("-----BEGIN CERTIFICATE-----\n"
                              "MIIBhTCCASugAwIBAgIUBENCHCA000000000000000000\n"
                              "-----END CERTIFICATE-----\n")},
}


def managed_region(text: str) -> str:
    lines = text.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == deploy.BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == deploy.END]
    assert len(begins) == 1 and len(ends) == 1, (begins, ends)
    return "\n".join(lines[begins[0]:ends[0] + 1])


class EnvFileTests(unittest.TestCase):
    def test_the_env_file_carries_the_bench_values(self) -> None:
        lines = [line for line in deploy.render_env().splitlines()
                 if line and not line.startswith("#")]
        self.assertEqual(lines, [
            "ODILE_BIND=127.0.0.1",
            "ODILE_PORT=8082",
            "ODILE_DB=/var/lib/odile-test/waitlist.db",
            "ODILE_ATTEST=/var/lib/odile-test/attestation.json",
            "ODILE_ROADMAP=/var/lib/odile-test/roadmap.html",
            "ODILE_SEED_BIND=127.0.0.1",
            "ODILE_SEED_PORT=8083",
            "ODILE_SEED_REBUILDS=/var/lib/odile-test/seed-rebuilds.json",
        ])

    def test_the_env_file_names_no_production_value_and_no_secret(self) -> None:
        text = deploy.render_env()
        body = "\n".join(line for line in text.splitlines()
                         if line and not line.startswith("#"))
        self.assertNotIn("8080", body)
        self.assertNotIn("8081", body)
        self.assertNotIn("/var/lib/odile/", body)
        self.assertNotIn("/root/.odile", body,
                         "a secret path belongs in a unit, not a 0644 file")
        self.assertNotIn("near", body.lower())
        self.assertNotIn("secret", body.lower())

    def test_the_env_file_is_stable_and_parameterised(self) -> None:
        first = deploy.render_env()
        self.assertEqual(first, deploy.render_env(),
                         "a second render must be byte-identical or every deploy writes")
        other = deploy.render_env(state=Path("/srv/other"), app_port=9082, seed_port=9083)
        self.assertIn("ODILE_PORT=9082", other)
        self.assertIn("ODILE_SEED_PORT=9083", other)
        self.assertIn("ODILE_DB=/srv/other/waitlist.db", other)


class UnitRenderTests(unittest.TestCase):
    def test_the_site_unit_runs_the_release_current(self) -> None:
        unit = deploy.render_unit(SITE_TEMPLATE)
        self.assertNotIn("{{", unit)
        self.assertIn("User=odile", unit)
        self.assertIn("WorkingDirectory=/opt/odile-test/current", unit)
        self.assertIn("StateDirectory=odile-test", unit)
        self.assertIn("EnvironmentFile=/etc/odile-bench/bench.env", unit)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/odile-test/current/app.py", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertNotIn("ODILE_DB=", unit,
                         "the values live in bench.env, one file an operator can read")

    def test_the_seed_unit_loads_the_bench_config_and_the_shared_key(self) -> None:
        unit = deploy.render_unit(SEED_TEMPLATE)
        self.assertNotIn("{{", unit)
        self.assertIn("LoadCredential=seed-config:/root/.odile/seed-config.test.json", unit)
        self.assertIn("LoadCredential=seed-key:/root/.odile/seed-signing.pem", unit)
        self.assertNotIn("seed-config.json\n", unit,
                         "the bench must not load production's seed config")
        self.assertIn("Environment=ODILE_SEED_KEYS=/var/lib/odile-test/seed-keys.json", unit)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/odile-test/current/seed/seed.py serve", unit)
        self.assertIn("StateDirectory=odile-test", unit)

    def test_the_register_unit_drains_the_queue_as_root(self) -> None:
        # ODL-341: the app runs as `odile`, the provider/DNS/key credentials do
        # not, so the queue's only order happens in this root oneshot.
        unit = deploy.render_unit(deploy.WORKER_SERVICE)
        self.assertNotIn("{{", unit)
        self.assertIn("User=root", unit)
        self.assertIn("ExecStart=/usr/bin/python3 -m odilectl.provision --worker --apply", unit)
        self.assertIn("--config /root/.odile/config.test.yaml", unit)
        self.assertIn("ReadWritePaths=/var/lib/odile-test /var/lib/odile", unit)
        timer = deploy.render_unit(deploy.WORKER_TIMER)
        self.assertIn("Unit=odile-bench-register", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_rendering_twice_is_identical_and_follows_the_values(self) -> None:
        first = deploy.render_unit(SITE_TEMPLATE)
        self.assertEqual(first, deploy.render_unit(SITE_TEMPLATE))
        other = deploy.render_unit(
            SITE_TEMPLATE, root=Path("/srv/bench"), state=Path("/var/lib/bench-state"),
            etc=Path("/etc/bench"), config=Path("/srv/config.json"),
            seed_key=Path("/srv/seed.pem"))
        self.assertIn("WorkingDirectory=/srv/bench/current", other)
        self.assertIn("StateDirectory=bench-state", other)
        self.assertIn("EnvironmentFile=/etc/bench/bench.env", other)
        self.assertNotIn("/opt/odile-test", other)


class SpliceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.block = BLOCK.read_text(encoding="utf-8")

    def test_the_fragment_is_one_marked_site_block(self) -> None:
        lines = [line.strip() for line in self.block.splitlines() if line.strip()]
        self.assertEqual(lines[0], deploy.BEGIN)
        self.assertEqual(lines[-1], deploy.END)
        self.assertEqual(self.block.count("test.odile.fyi {"), 1)
        self.assertEqual(self.block.count("reverse_proxy 127.0.0.1:8082"), 1)
        self.assertEqual(self.block.count("reverse_proxy 127.0.0.1:8083"), 1)
        self.assertNotIn("127.0.0.1:8080", self.block,
                         "the bench must never proxy to production's app port")
        deploy.check_site_block(deploy.splice_caddyfile(PRODUCTION_CADDY, self.block))

    def test_the_second_splice_is_byte_identical(self) -> None:
        once = deploy.splice_caddyfile(PRODUCTION_CADDY, self.block)
        twice = deploy.splice_caddyfile(once, self.block)
        self.assertEqual(once, twice)
        self.assertEqual(managed_region(once), managed_region(twice))
        self.assertEqual(once.count("test.odile.fyi {"), 1)

    def test_the_production_site_is_untouched(self) -> None:
        once = deploy.splice_caddyfile(PRODUCTION_CADDY, self.block)
        self.assertTrue(once.startswith(PRODUCTION_CADDY.rstrip("\n") + "\n\n"))
        self.assertIn(PRODUCTION_CADDY.rstrip("\n"), once)
        self.assertEqual(once.count("\nodile.fyi {"), 1)
        self.assertEqual(once.count("reverse_proxy 127.0.0.1:8081"), 1)
        self.assertEqual(once.count("reverse_proxy 127.0.0.1:8080"), 1)
        self.assertNotIn("127.0.0.1:8080", managed_region(once))

    def test_a_stale_block_is_replaced_not_duplicated(self) -> None:
        stale = PRODUCTION_CADDY + "\n" + self.block.replace("8082", "9999")
        once = deploy.splice_caddyfile(stale, self.block)
        self.assertNotIn("9999", once)
        self.assertEqual(once.count("test.odile.fyi {"), 1)
        self.assertEqual(once, deploy.splice_caddyfile(once, self.block))

    def test_duplicate_and_unbalanced_markers_are_refused(self) -> None:
        marked = deploy.splice_caddyfile(PRODUCTION_CADDY, self.block)
        with self.assertRaises(ValueError):
            deploy.splice_caddyfile(marked + "\n" + self.block, self.block)
        with self.assertRaises(ValueError):
            deploy.splice_caddyfile(PRODUCTION_CADDY + "\n" + deploy.BEGIN + "\n", self.block)

    def test_a_block_that_lost_a_route_is_refused(self) -> None:
        broken = self.block.replace(
            "\t@seed path /seed\n\thandle @seed {\n\t\treverse_proxy 127.0.0.1:8083\n\t}\n\n", "")
        with self.assertRaises(ValueError):
            deploy.check_site_block(deploy.splice_caddyfile(PRODUCTION_CADDY, broken))


class Recorder:
    """A fake host: records every command and simulates systemctl, caddy, curl."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.enabled = {name: "disabled" for name in deploy.UNIT_NAMES}
        self.active = {name: "inactive" for name in deploy.UNIT_NAMES}
        self.validate_rc = 0
        self.production = {url: f"production bytes for {url}"
                           for url in deploy.PRODUCTION_URLS}

    def __call__(self, command, check=False, timeout=None, env=None):
        self.calls.append(list(command))
        program = command[0]
        if program == "systemctl":
            verb = command[1]
            if verb == "is-enabled":
                return self._done(command, 0, self.enabled.get(command[2], ""))
            if verb == "is-active":
                return self._done(command, 0, self.active.get(command[2], ""))
            if verb == "enable":
                name = command[-1]
                self.enabled[name] = "enabled"
                self.active[name] = "active"
                return self._done(command, 0, "")
            if verb in ("start", "restart"):
                self.active[command[2]] = "active"
                return self._done(command, 0, "")
            if verb == "show":
                return self._done(command, 0, "NRestarts=0")
            return self._done(command, 0, "")
        if program == "caddy":
            return self._done(command, self.validate_rc, "")
        if program == "curl":
            url = command[-1]
            if url == deploy.APP_HEALTH:
                return self._done(command, 0, "200" if "-w" in command else "ok")
            if url == deploy.SEED_ROUTE_URL:
                return self._done(command, 0, "405")
            if url == deploy.LANDING_URL:
                return self._done(command, 0, "200")
            if url in self.production:
                if "-w" in command:
                    return self._done(command, 0, "200")
                return self._done(command, 0, self.production[url])
            return self._done(command, 0, "000")
        return self._done(command, 0, "")

    @staticmethod
    def _done(command, returncode, stdout):
        return subprocess.CompletedProcess(args=command, returncode=returncode,
                                           stdout=stdout, stderr="")

    def mutating(self) -> list[list[str]]:
        return [call for call in self.calls
                if (call[0] == "systemctl" and call[1] in
                    ("daemon-reload", "enable", "start", "restart", "stop"))
                or (call[0] == "caddy" and call[1] == "restart")]


class DeployerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "opt/odile-test"
        self.releases = self.root / "releases"
        self.current = self.root / "current"
        self.previous = self.root / "previous"
        self.state = base / "var/lib/odile-test"
        self.etc = base / "etc/odile-bench"
        self.env_file = self.etc / "bench.env"
        self.unit_site = base / "etc/systemd/system/odile-bench.service"
        self.unit_seed = base / "etc/systemd/system/odile-bench-seed.service"
        self.unit_probe = base / "etc/systemd/system/odile-bench-probe.service"
        self.timer_probe = base / "etc/systemd/system/odile-bench-probe.timer"
        self.unit_register = base / "etc/systemd/system/odile-bench-register.service"
        self.timer_register = base / "etc/systemd/system/odile-bench-register.timer"
        self.caddyfile = base / "etc/caddy/Caddyfile"
        self.config = base / "root/.odile/seed-config.test.json"
        self.prod_config = base / "root/.odile/seed-config.json"
        self.seed_key = base / "root/.odile/seed-signing.pem"
        self.token = base / "root/.odile/cloudflare_dns_token"
        self.db = self.state / "waitlist.db"
        self.roadmap = self.state / "roadmap.html"
        self.attest = self.state / "attestation.json"
        self.source = base / "src"
        for directory in (self.etc, self.caddyfile.parent, self.unit_site.parent,
                          self.config.parent, self.state, self.source,
                          self.source / "seed", self.source / "web"):
            directory.mkdir(parents=True, exist_ok=True)
        (self.source / "app.py").write_text("print('app')\n", encoding="utf-8")
        (self.source / "attest.py").write_text("print('attest')\n", encoding="utf-8")
        (self.source / "seed" / "seed.py").write_text("print('seed')\n", encoding="utf-8")
        (self.source / "web" / "index.html").write_text("<h1>Odile</h1>\n", encoding="utf-8")
        self.prod_config.write_text(json.dumps(PRODUCTION_CONFIG), encoding="utf-8")
        os.chmod(self.prod_config, 0o600)
        self.seed_key.write_text("fixture key\n", encoding="utf-8")
        self.token.write_text("fixture token\n", encoding="utf-8")
        self.caddyfile.write_text(PRODUCTION_CADDY, encoding="utf-8")
        # The roadmap cache exists, so the deployer prints `same` instead of
        # running the private generator (covered by its own test below).
        self.roadmap.write_text("bench roadmap\n", encoding="utf-8")
        self.recorder = Recorder()
        self.saved = {name: getattr(deploy, name) for name in (
            "ROOT", "RELEASES", "CURRENT", "PREVIOUS", "STATE", "DB_PATH",
            "ATTEST_PATH", "ROADMAP_PATH", "ROADMAP_SOURCE", "SEED_REBUILDS",
            "ETC", "ENV_FILE", "UNIT_SITE", "UNIT_SEED", "UNIT_PROBE",
            "TIMER_PROBE", "UNIT_REGISTER", "TIMER_REGISTER", "CADDYFILE",
            "CONFIG_FILE", "PROD_CONFIG", "SEED_KEY_FILE", "TOKEN_FILE",
            "DNS_IP", "ROADMAP_CANDIDATES", "run", "require_root",
            "service_ids", "chown", "resolve_a", "primary_ipv4",
            "port_holders", "is_bench_unit", "terminate",
            "PROBE_SECONDS", "CERT_SECONDS", "DNS_SECONDS")}
        deploy.ROOT = self.root
        deploy.RELEASES = self.releases
        deploy.CURRENT = self.current
        deploy.PREVIOUS = self.previous
        deploy.STATE = self.state
        deploy.DB_PATH = self.db
        deploy.ATTEST_PATH = self.attest
        deploy.ROADMAP_PATH = self.roadmap
        deploy.ROADMAP_SOURCE = self.state / "roadmap-source"
        deploy.SEED_REBUILDS = self.state / "seed-rebuilds.json"
        deploy.ETC = self.etc
        deploy.ENV_FILE = self.env_file
        deploy.UNIT_SITE = self.unit_site
        deploy.UNIT_SEED = self.unit_seed
        deploy.UNIT_PROBE = self.unit_probe
        deploy.TIMER_PROBE = self.timer_probe
        deploy.UNIT_REGISTER = self.unit_register
        deploy.TIMER_REGISTER = self.timer_register
        deploy.CADDYFILE = self.caddyfile
        deploy.CONFIG_FILE = self.config
        deploy.PROD_CONFIG = self.prod_config
        deploy.SEED_KEY_FILE = self.seed_key
        deploy.TOKEN_FILE = self.token
        deploy.DNS_IP = ""
        deploy.ROADMAP_CANDIDATES = ((), (), ())
        deploy.run = self.recorder
        deploy.require_root = lambda: None
        deploy.service_ids = lambda: (0, 0)
        deploy.chown = lambda path, ids: None
        deploy.primary_ipv4 = lambda: "203.0.113.139"
        deploy.resolve_a = lambda name: ["203.0.113.139"]
        # The live host's ports belong to the real bench; the fixture owns
        # its own answer, and the guard has its own tests below.
        deploy.port_holders = lambda port: []
        deploy.PROBE_SECONDS = 0.01
        deploy.CERT_SECONDS = 0.01
        deploy.DNS_SECONDS = 0.01

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(deploy, name, value)
        self.tmp.cleanup()

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = deploy.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                if isinstance(exc.code, str):
                    err.write(exc.code)
        return code, out.getvalue(), err.getvalue()

    def apply(self, *argv: str) -> tuple[int, str, str]:
        return self.run_main("--source", str(self.source), *argv)

    def test_dry_run_names_every_write_and_writes_nothing(self) -> None:
        self.roadmap.unlink()
        code, out, err = self.run_main("--dry-run", "--source", str(self.source),
                                       "--dns-ip", "203.0.113.9")
        self.assertEqual(code, 0, err)
        for label in ("would create", "would write", "would copy", "would point",
                      "would probe", "bench: planned"):
            self.assertIn(label, out)
        # Every destination the apply would touch is named.
        for path in (self.root, self.env_file, self.unit_site, self.unit_seed,
                     self.config, self.db, self.caddyfile):
            self.assertIn(str(path), out)
        self.assertIn(f"would write  test.odile.fyi A 203.0.113.9", out)
        self.assertIn(f"skip         {self.roadmap}", out)
        # And nothing was written, linked or restarted.
        self.assertFalse(self.root.exists())
        self.assertFalse(self.env_file.exists())
        self.assertFalse(self.config.exists())
        self.assertFalse(self.db.exists())
        self.assertFalse(self.current.is_symlink())
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"), PRODUCTION_CADDY)
        self.assertEqual(self.recorder.mutating(), [])

    def test_apply_installs_then_the_second_apply_is_nothing_to_do(self) -> None:
        code, out, err = self.apply()
        self.assertEqual(code, 0, err)
        self.assertIn("bench: deployed", out)

        # The environment file: the bench's values, 0644.
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), deploy.render_env())
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o644)
        # The units: rendered from the committed templates.
        self.assertEqual(self.unit_site.read_text(encoding="utf-8"),
                         deploy.render_unit(SITE_TEMPLATE))
        self.assertEqual(self.unit_seed.read_text(encoding="utf-8"),
                         deploy.render_unit(SEED_TEMPLATE))
        self.assertEqual(self.unit_probe.read_text(encoding="utf-8"),
                         deploy.render_unit(PROBE_TEMPLATE))
        self.assertEqual(self.timer_probe.read_text(encoding="utf-8"),
                         deploy.render_unit(TIMER_TEMPLATE))
        self.assertEqual(self.unit_register.read_text(encoding="utf-8"),
                         deploy.render_unit(deploy.WORKER_SERVICE))
        self.assertEqual(self.timer_register.read_text(encoding="utf-8"),
                         deploy.render_unit(deploy.WORKER_TIMER))
        self.assertIn("OnCalendar=*:0/5", self.timer_probe.read_text(encoding="utf-8"))
        self.assertIn("OnCalendar=*:0/2", self.timer_register.read_text(encoding="utf-8"))
        self.assertIn(["systemctl", "enable", "--now", "odile-bench-register.timer"],
                      self.recorder.calls)
        self.assertIn(["systemctl", "enable", "--now", "odile-bench-probe.timer"],
                      self.recorder.calls)
        # The bench database: its own path, empty, 0600 — production is not copied.
        self.assertTrue(self.db.is_file())
        self.assertEqual(self.db.stat().st_size, 0)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.db, Path("/var/lib/odile/waitlist.db"))
        # The seed config: 0600, derived, and not a byte copy of production.
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        bench = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(bench["environment"], "bench")
        self.assertTrue(bench["backup_repo_url"].endswith("/tenants/test-{slug}"))
        self.assertNotEqual(hashlib.sha256(self.config.read_bytes()).hexdigest(),
                            hashlib.sha256(self.prod_config.read_bytes()).hexdigest())
        # One release, `current` points at it, no `previous` on a first apply.
        self.assertTrue(self.current.is_symlink())
        link = os.readlink(self.current)
        self.assertTrue(link.startswith("releases/"))
        self.assertTrue((self.root / link).is_dir())
        self.assertFalse(self.previous.is_symlink())
        # Caddy: the block was appended once and the production part is unchanged.
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"),
                         deploy.splice_caddyfile(PRODUCTION_CADDY, BLOCK.read_text(encoding="utf-8")))
        self.assertGreaterEqual(len(list(self.caddyfile.parent.glob("Caddyfile.bak-bench-*"))), 1)
        self.assertEqual(self.recorder.active[deploy.UNIT_SITE_NAME], "active")

        # The second apply: same bytes, no restart, one line the DoD names.
        self.recorder.calls.clear()
        code, out, err = self.apply()
        self.assertEqual(code, 0, err)
        self.assertIn("bench: nothing to do", out)
        self.assertEqual(self.recorder.mutating(), [])
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"),
                         deploy.splice_caddyfile(PRODUCTION_CADDY, BLOCK.read_text(encoding="utf-8")))

    def test_one_rollback_returns_the_previous_release(self) -> None:
        self.assertEqual(self.apply()[0], 0)
        first = os.readlink(self.current)
        (self.source / "app.py").write_text("print('app v2')\n", encoding="utf-8")
        code, out, err = self.apply()
        self.assertEqual(code, 0, err)
        second = os.readlink(self.current)
        self.assertNotEqual(first, second)
        self.assertEqual(os.readlink(self.previous), first)

        self.recorder.calls.clear()
        code, out, err = self.run_main("--rollback")
        self.assertEqual(code, 0, err)
        self.assertIn("bench: rolled back", out)
        self.assertEqual(os.readlink(self.current), first)
        self.assertEqual(os.readlink(self.previous), second)
        self.assertIn(f"active       {deploy.UNIT_SITE_NAME} active", out)
        self.assertEqual(self.recorder.mutating(), [
            ["systemctl", "restart", deploy.UNIT_SITE_NAME],
            ["systemctl", "restart", deploy.UNIT_SEED_NAME],
        ])

    def test_rollback_dry_run_names_the_flip_and_changes_nothing(self) -> None:
        self.assertEqual(self.apply()[0], 0)
        first = os.readlink(self.current)
        (self.source / "app.py").write_text("print('app v2')\n", encoding="utf-8")
        self.assertEqual(self.apply()[0], 0)
        second = os.readlink(self.current)
        code, out, err = self.run_main("--rollback", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("rollback", out)
        self.assertIn("would point", out)
        self.assertEqual(os.readlink(self.current), second)
        self.assertEqual(os.readlink(self.previous), first)

    def test_rollback_without_a_previous_release_is_refused(self) -> None:
        self.assertEqual(self.apply()[0], 0)
        code, out, err = self.run_main("--rollback")
        self.assertEqual(code, 2)
        self.assertIn("no", err)
        self.assertIn(str(self.previous), err)

    def test_a_rejected_caddyfile_is_restored(self) -> None:
        self.recorder.validate_rc = 1
        code, out, err = self.apply()
        self.assertEqual(code, 1)
        self.assertIn("restored it", err)
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"), PRODUCTION_CADDY)

    def test_dns_is_created_once_and_then_reads_same(self) -> None:
        address = {"value": ""}
        calls = []

        def resolve(name):
            return [address["value"]] if address["value"] else []

        def api(token, method, path, body=None):
            calls.append((method, path, body))
            if path.startswith("/zones?"):
                return {"success": True, "result": [{"id": "zone1", "name": "odile.fyi"}]}
            if method == "GET":
                return {"success": True, "result": []}
            address["value"] = body["content"]
            return {"success": True, "result": {"id": "record1"}}

        deploy.resolve_a = resolve
        deploy.cloudflare_api = api
        code, out, err = self.apply("--dns-ip", "203.0.113.9")
        self.assertEqual(code, 0, err)
        self.assertIn("create       test.odile.fyi A 203.0.113.9 (DNS-only)", out)
        create = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0][2]["proxied"], False)
        self.assertEqual(create[0][2]["content"], "203.0.113.9")

        calls.clear()
        code, out, err = self.apply("--dns-ip", "203.0.113.9")
        self.assertEqual(code, 0, err)
        self.assertIn("same         test.odile.fyi A 203.0.113.9 (DNS-only)", out)
        self.assertEqual(calls, [])


class BenchConfigTests(unittest.TestCase):
    def test_the_bench_config_is_derived_without_mutating_production(self) -> None:
        before = json.dumps(PRODUCTION_CONFIG, sort_keys=True)
        bench = deploy.bench_config(PRODUCTION_CONFIG)
        self.assertEqual(json.dumps(PRODUCTION_CONFIG, sort_keys=True), before)
        self.assertEqual(set(bench), set(PRODUCTION_CONFIG) | {"environment"})
        self.assertEqual(bench["near_api_key"], PRODUCTION_CONFIG["near_api_key"])
        self.assertEqual(bench["mail_relay"], PRODUCTION_CONFIG["mail_relay"])
        self.assertNotEqual(bench["backup_repo_url"], PRODUCTION_CONFIG["backup_repo_url"])
        self.assertEqual(bench["environment"], "bench")


class PortGuardTests(unittest.TestCase):
    """ODL-936: a foreign holder is refused, a deleted worktree's orphan is reaped."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.saved = (deploy.port_holders, deploy.is_bench_unit, deploy.terminate)
        deploy.is_bench_unit = lambda pid: False
        self.terminated: list[int] = []
        deploy.terminate = lambda pid: self.terminated.append(pid)

    def tearDown(self) -> None:
        deploy.port_holders, deploy.is_bench_unit, deploy.terminate = self.saved
        self.tmp.cleanup()

    @staticmethod
    def holder(cwd: Path, pid: int = 4242) -> list[dict]:
        return [{"pid": pid, "command": "python3 -m http.server 8082",
                 "cwd": str(cwd)}]

    def test_a_live_foreign_holder_is_refused_and_named(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                deploy.ensure_port_free(False, (8082,),
                                        holders=lambda port: self.holder(self.base))
        self.assertEqual(caught.exception.code, 2)
        message = err.getvalue()
        self.assertIn("pid 4242", message)
        self.assertIn("python3 -m http.server 8082", message)
        self.assertIn("not odile-bench", message)
        self.assertEqual(self.terminated, [])
        self.assertNotIn("would reap", out.getvalue())

    def test_an_orphan_whose_worktree_is_gone_is_reaped(self) -> None:
        worktree = self.base / ".worktrees" / "t_gone"
        (worktree / "sub").mkdir(parents=True)
        cwd = worktree / "sub"
        shutil.rmtree(worktree)
        state = {"held": True}

        def holders(port: int) -> list[dict]:
            return self.holder(cwd) if state["held"] else []

        def terminate(pid: int) -> None:
            self.terminated.append(pid)
            state["held"] = False

        deploy.terminate = terminate
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed = deploy.ensure_port_free(False, (8082,), holders=holders)
        self.assertTrue(changed)
        self.assertIn("whose worktree is gone", out.getvalue())
        self.assertIn("reap", out.getvalue())
        self.assertEqual(self.terminated, [4242])

    def test_the_dry_run_names_the_reap_and_kills_nothing(self) -> None:
        worktree = self.base / ".worktrees" / "t_gone"
        worktree.mkdir(parents=True)
        cwd = worktree / "sub"
        worktree.rename(self.base / "moved")  # cwd's parent is gone
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed = deploy.ensure_port_free(True, (8082,),
                                              holders=lambda port: self.holder(cwd))
        self.assertTrue(changed)
        self.assertIn("would reap", out.getvalue())
        self.assertEqual(self.terminated, [])

    def test_the_benchs_own_holder_is_the_healthy_case(self) -> None:
        deploy.is_bench_unit = lambda pid: True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed = deploy.ensure_port_free(False, (8082,),
                                              holders=lambda port: self.holder(self.base))
        self.assertFalse(changed)
        self.assertIn("held by odile-bench", out.getvalue())
        self.assertEqual(self.terminated, [])


class ProbeTickTests(unittest.TestCase):
    """ODL-936: one line every tick; red feeds the HQ alert path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.script = self.base / "check_bench.sh"
        self.script.write_text("#!/bin/sh\n", encoding="utf-8")
        self.saved = (deploy.PROBE_SCRIPT, deploy.PROBE_TIMEOUT, deploy.run,
                      deploy.open_bench_alert, deploy.resolve_bench_alert,
                      deploy.port_holders, deploy.is_bench_unit, deploy.terminate)
        deploy.PROBE_SCRIPT = self.script
        # The tick looks at the live port before it probes; keep the test off
        # this host's 8082 unless a test puts a holder there.
        deploy.port_holders = lambda port: []
        deploy.is_bench_unit = lambda pid: False
        self.terminated: list[int] = []
        deploy.terminate = lambda pid: self.terminated.append(pid)
        self.opened: list[str] = []
        self.resolved: list[int] = []
        deploy.open_bench_alert = lambda line, now: self.opened.append(line)
        deploy.resolve_bench_alert = lambda now: (self.resolved.append(now), True)[1]

    def tearDown(self) -> None:
        (deploy.PROBE_SCRIPT, deploy.PROBE_TIMEOUT, deploy.run,
         deploy.open_bench_alert, deploy.resolve_bench_alert,
         deploy.port_holders, deploy.is_bench_unit, deploy.terminate) = self.saved
        self.tmp.cleanup()

    def tick(self, returncode: int, text: str) -> tuple[int, str]:
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(
                args=command, returncode=returncode,
                stdout=text if returncode == 0 else "",
                stderr="" if returncode == 0 else text)

        deploy.run = fake_run
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = deploy.probe_tick()
        return code, out.getvalue()

    def test_a_red_probe_prints_its_one_line_and_raises_the_alert(self) -> None:
        line = ("bench probe: FAIL https://test.odile.fyi/ -> 404 server: none "
                "(want 200 with server: odile-bench); 127.0.0.1:8082 pid 77 "
                "/usr/bin/python3 seed/seed.py serve")
        code, out = self.tick(1, line)
        self.assertEqual(code, 1)
        self.assertEqual(out.count(line), 1)
        self.assertEqual(self.opened, [line])
        self.assertEqual(self.resolved, [])

    def test_a_green_probe_prints_one_line_and_resolves_the_alert(self) -> None:
        line = ("bench probe: OK https://test.odile.fyi/ -> 200 server: odile-bench; "
                "127.0.0.1:8082 pid 9 /usr/bin/python3 /opt/odile-test/current/app.py")
        code, out = self.tick(0, line)
        self.assertEqual(code, 0)
        self.assertEqual(out.count(line), 1)
        self.assertEqual(self.opened, [])
        self.assertEqual(len(self.resolved), 1)

    def test_a_missing_probe_is_its_own_exit_and_names_the_path(self) -> None:
        deploy.PROBE_SCRIPT = self.base / "missing.sh"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = deploy.probe_tick()
        self.assertEqual(code, 2)
        self.assertIn(str(deploy.PROBE_SCRIPT), out.getvalue())

    def test_the_timer_path_needs_no_root(self) -> None:
        deploy.PROBE_SCRIPT = self.script
        deploy_run = deploy.run
        deploy.run = lambda command, **kwargs: subprocess.CompletedProcess(
            args=command, returncode=0, stdout="bench probe: OK line\n", stderr="")
        calls = []
        saved_root = deploy.require_root
        deploy.require_root = lambda: calls.append("root")
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = deploy.main(["--probe-tick"])
        finally:
            deploy.require_root = saved_root
            deploy.run = deploy_run
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_a_free_port_adds_no_line_to_the_probe(self) -> None:
        deploy.resolve_bench_alert = lambda now: False  # no open alert
        line = ("bench probe: OK https://test.odile.fyi/ -> 200 server: odile-bench; "
                "127.0.0.1:8082 pid 9 /usr/bin/python3 /opt/odile-test/current/app.py")
        code, out = self.tick(0, line)
        self.assertEqual(code, 0)
        self.assertEqual(out, line + "\n")

    def test_the_tick_reaps_a_deleted_worktrees_orphan_and_starts_the_unit(self) -> None:
        """The 2026-11-21 orphan must die with its worktree, not with a deploy."""
        worktree = self.base / ".worktrees" / "t_gone"
        (worktree / "sub").mkdir(parents=True)
        cwd = worktree / "sub"
        shutil.rmtree(worktree)
        state = {"held": True}
        deploy.port_holders = lambda port: (
            [{"pid": 4242, "command": "seed.py serve", "cwd": str(cwd)}]
            if state["held"] else []
        )

        def terminate(pid: int) -> None:
            self.terminated.append(pid)
            state["held"] = False

        deploy.terminate = terminate
        line = ("bench probe: OK https://test.odile.fyi/ -> 200 server: odile-bench; "
                "127.0.0.1:8082 pid 9 /usr/bin/python3 /opt/odile-test/current/app.py")
        commands: list[list[str]] = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[-1] == deploy.APP_HEALTH:
                return subprocess.CompletedProcess(
                    args=command, returncode=0, stdout="ok\n", stderr="")
            if command[0] == deploy.SYSTEMCTL and command[1:2] == ["is-active"]:
                return subprocess.CompletedProcess(
                    args=command, returncode=0, stdout="active\n", stderr="")
            if command[0] == deploy.SYSTEMCTL and command[1:2] == ["start"]:
                return subprocess.CompletedProcess(
                    args=command, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout=line + "\n", stderr="")

        deploy.run = fake_run
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = deploy.probe_tick()
        shown = out.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(self.terminated, [4242])
        self.assertIn("whose worktree is gone", shown)
        self.assertIn([deploy.SYSTEMCTL, "start", deploy.UNIT_SITE_NAME], commands)
        self.assertEqual(shown.count(line), 1)
        self.assertLess(shown.index("whose worktree is gone"), shown.index(line))
        self.assertEqual(self.opened, [])
        self.assertEqual(len(self.resolved), 1)

    def test_the_tick_leaves_a_foreign_holder_to_the_deployers_refusal(self) -> None:
        """A stranger on 8082 is not the tick's to kill: it probes red and alerts."""
        deploy.port_holders = lambda port: [{
            "pid": 5151, "command": "python3 -m http.server 8082",
            "cwd": str(self.base)}]
        line = ("bench probe: FAIL https://test.odile.fyi/ -> 200 server: odile-bench, "
                "account page: no (want 200, server: odile-bench, the join form); "
                "127.0.0.1:8082 pid 5151 python3 -m http.server 8082")
        code, out = self.tick(1, line)
        self.assertEqual(code, 1)
        self.assertEqual(self.terminated, [])
        self.assertEqual(out.count(line), 1)
        self.assertIn("pid 5151", out)
        self.assertEqual(self.opened, [line])
        self.assertEqual(self.resolved, [])


if __name__ == "__main__":
    unittest.main()
