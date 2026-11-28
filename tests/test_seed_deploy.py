"""Tests for the seed-service deployer (ODL-332 follow-up).

ODL-204 built the seed service, but the unit and the Caddy route lived only in
``docs/runbooks/seed.md`` §3, applied to HQ by hand. A rebuilt HQ therefore had
no ``/seed`` and every tenant install failed at step 3. The deployer owns both
now, and these tests hold the two properties that make it safe to run on a live
host:

* every Caddyfile shape a host can be in — a fresh bare ``reverse_proxy``, a
  fresh ``handle { ... }`` block, and the hand-applied seed block that is live
  today — converges to the same managed region, and a second splice is a no-op;
* the apply path installs the unit, enables it, splices the route once, backs
  the Caddyfile up before writing it, and a converged host writes nothing and
  runs no mutating command.

The filesystem tests run the real ``main()`` with every path pointed at a
throwaway tree and ``run`` replaced by a recorder, so they neither need nor
touch the live host, systemd or Caddy.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "seed" / "deploy_seed.py"
FRAGMENT = ROOT / "deploy" / "seed" / "odile-seed.caddy"
UNIT_TEMPLATE = ROOT / "deploy" / "seed" / "odile-seed.service"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load("odile_deploy_seed", SCRIPT)

# The host's site block before ODL-204: one bare proxy. This is the shape in
# /etc/caddy/Caddyfile.bak-20261109T192416Z, i.e. a rebuilt HQ.
FRESH_BARE = """\
{
\tadmin off
}

odile.fyi {
\tencode zstd gzip

\theader {
\t\tServer odile
\t\tStrict-Transport-Security "max-age=31536000; includeSubDomains"
\t}

\tlog {
\t\toutput discard
\t}

\treverse_proxy 127.0.0.1:8080
}

www.odile.fyi {
\theader Server odile
\tredir https://odile.fyi{uri} permanent
}
"""

# A host whose app route is already an explicit catch-all handler.
FRESH_HANDLE = FRESH_BARE.replace(
    "\treverse_proxy 127.0.0.1:8080\n",
    "\thandle {\n\t\treverse_proxy 127.0.0.1:8080\n\t}\n",
)

# The route ODL-332 applied by hand on 2026-11-09: unmarked, no BEGIN/END.
HAND_APPLIED = FRESH_BARE.replace(
    "\treverse_proxy 127.0.0.1:8080\n",
    "\t# ODL-204/ODL-332: POST /seed is the one route to the seed service.\n"
    "\t# The token travels in the body, never in the URL, so no proxy log sees it.\n"
    "\t@seed path /seed\n"
    "\thandle @seed {\n"
    "\t\treverse_proxy 127.0.0.1:8081\n"
    "\t}\n"
    "\n"
    "\thandle {\n"
    "\t\treverse_proxy 127.0.0.1:8080\n"
    "\t}\n",
)

SHAPES = {
    "fresh-bare": FRESH_BARE,
    "fresh-handle": FRESH_HANDLE,
    "hand-applied": HAND_APPLIED,
}


def managed_region(text: str) -> str:
    """The lines from the BEGIN marker to the END marker, inclusive."""
    lines = text.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == deploy.BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == deploy.END]
    assert len(begins) == 1 and len(ends) == 1, (begins, ends)
    return "\n".join(lines[begins[0]:ends[0] + 1])


class FragmentTests(unittest.TestCase):
    def test_fragment_is_one_marked_region(self) -> None:
        text = FRAGMENT.read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        self.assertEqual(lines[0], deploy.BEGIN)
        self.assertEqual(lines[-1], deploy.END)
        self.assertEqual(lines.count(deploy.BEGIN), 1)
        self.assertEqual(lines.count(deploy.END), 1)

    def test_fragment_routes_seed_and_keeps_the_app_catch_all(self) -> None:
        body = FRAGMENT.read_text(encoding="utf-8")
        self.assertIn("@seed path /seed", body)
        self.assertIn("reverse_proxy 127.0.0.1:8081", body)
        self.assertIn("reverse_proxy 127.0.0.1:8080", body)
        # The catch-all is the last route: /seed must not shadow the app.
        self.assertLess(body.index("@seed path /seed"),
                        body.index("reverse_proxy 127.0.0.1:8080"))

    def test_unit_template_names_the_two_credentials(self) -> None:
        unit = UNIT_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("User=odile", unit)
        self.assertIn("LoadCredential=seed-config:/root/.odile/seed-config.json", unit)
        self.assertIn("LoadCredential=seed-key:/root/.odile/seed-signing.pem", unit)
        self.assertNotIn("installer-signing", unit,
                         "the seed service does not load the release key (ODL-514)")
        self.assertIn("WantedBy=multi-user.target", unit)


class SpliceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fragment = FRAGMENT.read_text(encoding="utf-8")

    def test_every_shape_converges_to_one_region(self) -> None:
        regions = {
            name: managed_region(deploy.splice_caddyfile(text, self.fragment))
            for name, text in SHAPES.items()
        }
        self.assertEqual(len(set(regions.values())), 1, regions)

    def test_spliced_region_carries_both_routes(self) -> None:
        region = managed_region(deploy.splice_caddyfile(FRESH_BARE, self.fragment))
        self.assertIn("@seed path /seed", region)
        self.assertIn("reverse_proxy 127.0.0.1:8081", region)
        self.assertIn("reverse_proxy 127.0.0.1:8080", region)

    def test_splice_is_idempotent(self) -> None:
        for name, text in SHAPES.items():
            with self.subTest(shape=name):
                once = deploy.splice_caddyfile(text, self.fragment)
                twice = deploy.splice_caddyfile(once, self.fragment)
                self.assertEqual(once, twice)
                self.assertEqual(managed_region(once), managed_region(twice))

    def test_hand_applied_block_is_replaced_not_duplicated(self) -> None:
        result = deploy.splice_caddyfile(HAND_APPLIED, self.fragment)
        self.assertEqual(result.count("@seed path /seed"), 1)
        self.assertEqual(result.count("reverse_proxy 127.0.0.1:8081"), 1)
        self.assertEqual(result.count("handle {"), 1)
        # The hand-applied comment block is adopted, not kept above the marker.
        self.assertEqual(result.count("# ODL-204/ODL-332: POST /seed is the one route"), 1)

    def test_rest_of_the_file_is_untouched(self) -> None:
        result = deploy.splice_caddyfile(FRESH_BARE, self.fragment)
        prefix = FRESH_BARE.split("\treverse_proxy 127.0.0.1:8080")[0]
        self.assertTrue(result.startswith(prefix))
        self.assertTrue(result.endswith("www.odile.fyi {\n"
                                        "\theader Server odile\n"
                                        "\tredir https://odile.fyi{uri} permanent\n"
                                        "}\n"))

    def test_markers_are_indented_with_the_block_body(self) -> None:
        result = deploy.splice_caddyfile(FRESH_BARE, self.fragment)
        self.assertIn("\n\t" + deploy.BEGIN + "\n", result)
        self.assertIn("\n\t" + deploy.END + "\n", result)
        self.assertIn("\n\t@seed path /seed\n", result)

    def test_a_second_site_block_with_its_own_handle_still_splices(self) -> None:
        # The live file has another site block with its own matcher and its own
        # `handle { ... }`: the route check must look inside odile.fyi only, not
        # count the whole file.
        two_sites = FRESH_BARE + (
            "\ndemo.odile.fyi {\n"
            "\t@other {\n\t\tmethod POST\n\t}\n"
            "\thandle @other {\n\t\trespond 403\n\t}\n"
            "\thandle {\n\t\treverse_proxy 127.0.0.1:9130\n\t}\n"
            "}\n"
        )
        result = deploy.splice_caddyfile(two_sites, self.fragment)
        self.assertEqual(result.count("@seed path /seed"), 1)
        self.assertEqual(result.count("reverse_proxy 127.0.0.1:9130"), 1)
        # The demo block is carried over byte for byte.
        self.assertIn("\ndemo.odile.fyi {" + two_sites.split("\ndemo.odile.fyi {")[1], result)

    def test_duplicate_markers_are_refused(self) -> None:
        marked = deploy.splice_caddyfile(FRESH_BARE, self.fragment)
        doubled = marked + "\n" + self.fragment
        with self.assertRaises(ValueError):
            deploy.splice_caddyfile(doubled, self.fragment)

    def test_unbalanced_marker_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            deploy.splice_caddyfile(FRESH_BARE + "\n" + deploy.BEGIN + "\n", self.fragment)

    def test_missing_site_block_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            deploy.splice_caddyfile("example.com {\n\treverse_proxy 127.0.0.1:9\n}\n",
                                    self.fragment)

    def test_unknown_route_tail_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            deploy.splice_caddyfile("odile.fyi {\n\tencode gzip\n}\n", self.fragment)


class Recorder:
    """A fake host: records every command and simulates systemctl, caddy, curl."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.enabled = "disabled"
        self.active = "inactive"
        self.validate_rc = 0
        self.health = "ok"
        self.codes = {deploy.SEED_ROUTE_URL: "405", deploy.LANDING_URL: "200"}

    def __call__(self, command, check=False, **_kwargs):
        self.calls.append(list(command))
        program = command[0]
        if program == "systemctl":
            verb = command[1]
            if verb == "is-enabled":
                return self._done(command, 0, self.enabled)
            if verb == "enable":
                self.enabled, self.active = "enabled", "active"
                return self._done(command, 0, "")
            if verb == "is-active":
                return self._done(command, 0, self.active)
            if verb in ("start", "restart"):
                self.active = "active"
                return self._done(command, 0, "")
            return self._done(command, 0, "")
        if program == "caddy":
            return self._done(command, self.validate_rc, "")
        if program == "curl":
            url = command[-1]
            if url == deploy.SEED_HEALTH_URL:
                return self._done(command, 0, self.health)
            return self._done(command, 0, self.codes.get(url, "000"))
        return self._done(command, 0, "")

    @staticmethod
    def _done(command, returncode, stdout):
        return subprocess.CompletedProcess(args=command, returncode=returncode,
                                           stdout=stdout, stderr="")

    def mutating(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] != "curl" and not (
            c[0] == "systemctl" and c[1] in ("is-enabled", "is-active"))]


class DeployerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.unit = root / "etc/systemd/system/odile-seed.service"
        self.caddyfile = root / "etc/caddy/Caddyfile"
        self.config = root / "root/.odile/seed-config.json"
        self.key = root / "root/.odile/seed-signing.pem"
        self.release = root / "offline/release.pem"
        self.release_pub = root / "offline/release.pub"
        self.pub = root / "deploy/seed/seed-key.pub"
        self.cert = root / "deploy/seed/seed-key.crt"
        self.runtime_seed = root / "opt/odile/seed/seed.py"
        for path in (self.unit.parent, self.caddyfile.parent, self.config.parent,
                     self.release.parent, self.pub.parent):
            path.mkdir(parents=True, exist_ok=True)
        for path in (self.config,):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        # ODL-514: a seed key (created the way the deployer creates one) and a
        # release key's certificate over its public half.
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.key)],
                       check=True, capture_output=True)
        os.chmod(self.key, 0o600)
        subprocess.run(["openssl", "pkey", "-in", str(self.key), "-pubout", "-out", str(self.pub)],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.release)],
                       check=True, capture_output=True)
        os.chmod(self.release, 0o600)
        subprocess.run(["openssl", "pkey", "-in", str(self.release), "-pubout",
                        "-out", str(self.release_pub)], check=True, capture_output=True)
        subprocess.run(["openssl", "dgst", "-sign", str(self.release), "-out", str(self.cert),
                        str(self.pub)], check=True, capture_output=True)
        os.chmod(self.cert, 0o644)
        self.caddyfile.write_text(deploy.splice_caddyfile(
            FRESH_BARE, FRAGMENT.read_text(encoding="utf-8")), encoding="utf-8")
        self.recorder = Recorder()
        self.saved = {name: getattr(deploy, name) for name in (
            "UNIT_TARGET", "CADDYFILE", "CONFIG_FILE", "SEED_KEY_FILE", "SEED_PUB",
            "SEED_CERT", "RELEASE_PUBKEY", "RUNTIME_SEED",
            "BACKUP_PAIRS", "run",
            "require_root", "PROBE_SECONDS")}
        deploy.UNIT_TARGET = self.unit
        deploy.CADDYFILE = self.caddyfile
        deploy.CONFIG_FILE = self.config
        deploy.SEED_KEY_FILE = self.key
        deploy.SEED_PUB = self.pub
        deploy.SEED_CERT = self.cert
        deploy.RELEASE_PUBKEY = self.release_pub
        deploy.RUNTIME_SEED = self.runtime_seed
        # ODL-924: the per-tenant pair file too, or the deploy reads the live
        # host's and a converged run prints its entry count.
        deploy.BACKUP_PAIRS = Path(self.tmp.name) / "seed-backup-pairs.json"
        deploy.run = self.recorder
        deploy.require_root = lambda: None
        deploy.PROBE_SECONDS = 0.01

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(deploy, name, value)
        self.tmp.cleanup()

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = deploy.main(list(argv))
            except SystemExit as exc:  # a hard failure: the message is the reason
                code = exc.code if isinstance(exc.code, int) else 1
                if isinstance(exc.code, str):
                    err.write(exc.code)
        return code, out.getvalue(), err.getvalue()

    def test_fresh_host_installs_enables_splices_and_restarts_caddy(self) -> None:
        # A fresh box: no unit, no route, no previous seed module.
        self.caddyfile.write_text(FRESH_BARE, encoding="utf-8")
        code, out, err = self.run_main("--previous-seed-sha", "")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.unit.read_bytes(), UNIT_TEMPLATE.read_bytes())
        self.assertIn("@seed path /seed", self.caddyfile.read_text(encoding="utf-8"))
        self.assertEqual([c[0:2] for c in self.recorder.mutating()], [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable"],
            ["caddy", "validate"],
            ["systemctl", "restart"],
        ])
        self.assertIn("caddy", self.recorder.mutating()[3])
        self.assertIn("seed: deployed", out)
        backups = list(self.caddyfile.parent.glob("Caddyfile.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), FRESH_BARE)

    def test_converged_host_changes_nothing(self) -> None:
        # The unit is installed and enabled, the route is spliced, the module
        # hash matches: every managed line says `same` and nothing is written.
        self.unit.write_bytes(UNIT_TEMPLATE.read_bytes())
        self.caddyfile.write_text(deploy.splice_caddyfile(
            FRESH_BARE, FRAGMENT.read_text(encoding="utf-8")), encoding="utf-8")
        self.runtime_seed.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_seed.write_text("# seed\n", encoding="utf-8")
        before_unit = self.unit.stat().st_mtime_ns
        before_caddy = self.caddyfile.stat().st_mtime_ns
        self.recorder.enabled, self.recorder.active = "enabled", "active"
        digest = hashlib.sha256(self.runtime_seed.read_bytes()).hexdigest()

        code, out, err = self.run_main("--previous-seed-sha", digest)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.recorder.mutating(), [])
        self.assertIn("same", out)
        self.assertIn("seed: converged", out)
        self.assertEqual(self.unit.stat().st_mtime_ns, before_unit)
        self.assertEqual(self.caddyfile.stat().st_mtime_ns, before_caddy)
        self.assertEqual(list(self.caddyfile.parent.glob("Caddyfile.bak-*")), [])
        for line in out.splitlines():
            if line.startswith(("same", "healthy", "route", "page", "certificate", "seed:")):
                continue
            self.fail(f"unexpected line on a converged host: {line!r}")

    def test_a_missing_seed_key_is_created_and_a_stale_certificate_stops_the_run(self) -> None:
        """A rebuilt HQ gets a key; without a certificate for it the deploy stops."""
        self.key.unlink()
        self.pub.unlink()
        code, out, err = self.run_main()
        self.assertEqual(code, 2, err)
        self.assertIn("make seed-cert", err)
        self.assertTrue(self.key.is_file(), "the deploy creates the seed key when it is absent")
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.pub.read_bytes(), b"", "the public half was written too")
        self.assertEqual(self.recorder.mutating(), [],
                         "nothing is installed when the key is uncertified")

    def test_a_certificate_for_another_key_stops_the_deploy(self) -> None:
        other = Path(self.tmp.name) / "offline/other.pub"
        subprocess.run(["openssl", "pkey", "-in", str(self.release), "-pubout",
                        "-out", str(other)], check=True, capture_output=True)
        subprocess.run(["openssl", "dgst", "-sign", str(self.release), "-out", str(self.cert),
                        str(other)], check=True, capture_output=True)
        code, _out, err = self.run_main()
        self.assertEqual(code, 2, err)
        self.assertIn("does not verify against", err)
        self.assertIn("make seed-cert", err)
        self.assertEqual(self.recorder.mutating(), [])

    def test_changed_seed_module_restarts_the_service(self) -> None:
        self.unit.write_bytes(UNIT_TEMPLATE.read_bytes())
        self.runtime_seed.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_seed.write_text("# new seed code\n", encoding="utf-8")
        self.recorder.enabled, self.recorder.active = "enabled", "active"
        code, out, err = self.run_main("--previous-seed-sha", "0" * 64)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.recorder.mutating(), [["systemctl", "restart", "odile-seed"]])
        self.assertIn("restart", out)

    def test_dry_run_writes_nothing_and_runs_nothing(self) -> None:
        self.caddyfile.write_text(FRESH_BARE, encoding="utf-8")
        code, out, err = self.run_main("--dry-run", "--previous-seed-sha", "")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.recorder.mutating(), [])
        self.assertFalse(self.unit.exists())
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"), FRESH_BARE)
        self.assertIn("would write", out)
        self.assertIn("would probe", out)
        self.assertIn("seed: planned", out)

    def test_missing_credential_is_refused(self) -> None:
        self.config.unlink()
        code, _out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn(str(self.config), err)
        self.assertIn("docs/runbooks/seed.md", err)
        self.assertEqual(self.recorder.calls, [])

    def test_group_readable_credential_is_refused(self) -> None:
        self.config.chmod(0o644)
        code, _out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("chmod 600", err)
        self.assertEqual(self.recorder.calls, [])

    def test_failed_caddy_validate_restores_the_previous_file(self) -> None:
        self.caddyfile.write_text(FRESH_BARE, encoding="utf-8")
        self.recorder.validate_rc = 1
        code, _out, err = self.run_main("--previous-seed-sha", "")
        self.assertEqual(code, 1)
        self.assertIn("caddy validate", err)
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"), FRESH_BARE)
        self.assertNotIn(["systemctl", "restart", "caddy"],
                         [c[0:3] for c in self.recorder.mutating()])


if __name__ == "__main__":
    unittest.main()
