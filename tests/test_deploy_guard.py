#!/usr/bin/python3
"""The deployer refuses a checkout that does not contain `main`.

On 2026-11-11 two deploys from pre-merge branch trees replaced `/opt/odile`
with a tree that lacked the merged `/roadmap` route, and the page answered 404
until a main deploy restored it. `scripts/deploy.sh` now proves its HEAD contains
main's tip before the first write and names the commits that are missing.

These tests build throwaway git repositories — a main line, and branches that
do and do not contain it — and run the real script with PATH shims for `getent`
and `rsync`. The shims mean no fixture can reach `/opt/odile` or systemd, and
the absence of their marker is how a refusal proves it wrote nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts/deploy.sh"
HQ_TOOLS = ROOT / "scripts/hq_tools.sh"

# `git -c` pairs so a fixture commit needs no user config on the host.
GIT_IDENTITY = (
    "-c",
    "user.name=Odile Deploy Guard",
    "-c",
    "user.email=deploy-guard@odile.fyi",
)


def git_env() -> dict[str, str]:
    """The ambient environment minus every inherited git handle.

    `make check-fast` runs from `.githooks/pre-commit`, which exports `GIT_DIR`
    and `GIT_INDEX_FILE` for the outer repository. A fixture that inherits them
    commits onto the card's branch or reads the wrong ref, so both the fixture
    git calls and the script under test get a scrubbed environment.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def short(checkout: "Checkout", rev: str) -> str:
    """The short sha the script prints for a revision."""
    return checkout.git("rev-parse", "--short", rev).stdout.strip()


def advance_main(checkout: "Checkout") -> dict[str, str]:
    """Move main two commits ahead and return to the caller's checkout.

    The two commits stand in for the merged work a behind-branch deploy would
    silently remove: the roadmap route and the plan entry that named it.
    """
    current = checkout.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    checkout.git("checkout", "--quiet", "main")
    checkout.write("tools/roadmap_page.py", "# roadmap generator\n")
    roadmap = checkout.commit("Merge ODL-490 — roadmap page at odile.fyi/roadmap")
    checkout.write("docs/backlog.yaml", "tasks: []\n")
    plan = checkout.commit("plan: add ODL-334 for M0")
    checkout.git("checkout", "--quiet", current)
    return {"roadmap": short(checkout, roadmap), "plan": short(checkout, plan)}


class Checkout:
    """A throwaway git repository holding a copy of the real deploy.sh."""

    def __init__(self, root: Path, branch: str = "main") -> None:
        self.repo = root / "checkout"
        self.repo.mkdir(parents=True)
        self.shims = root / "shims"
        self.shims.mkdir()
        self.marker = root / "rsync-called"
        self.git("init", "--quiet", "--initial-branch", branch)
        (self.repo / "app.py").write_text("# odile\n", encoding="utf-8")
        (self.repo / "scripts").mkdir(parents=True, exist_ok=True)
        deploy = self.repo / "scripts/deploy.sh"
        deploy.write_bytes(DEPLOY.read_bytes())
        deploy.chmod(0o755)
        # The HQ-only-tools rule ships beside the entry point; deploy.sh
        # refuses a checkout that lacks it (ODL-347-F1-F1).
        helper = self.repo / "scripts/hq_tools.sh"
        helper.write_bytes(HQ_TOOLS.read_bytes())
        helper.chmod(0o755)
        self.commit("base tree")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=git_env(),
            check=True,
            capture_output=True,
            text=True,
        )

    def commit(self, message: str) -> str:
        self.git("add", "--all")
        self.git(
            *GIT_IDENTITY,
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            message,
        )
        return self.git("rev-parse", "HEAD").stdout.strip()

    def write(self, name: str, text: str = "# fixture\n") -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def shim(
        self,
        *,
        getent: bool,
        rsync_status: int = 1,
        real_rsync: Path | None = None,
        runtime: Path | None = None,
    ) -> None:
        """Put read-only stand-ins for `getent` and `rsync` on PATH.

        `getent` chooses whether the script may reach the pre-guard group
        check. By default `rsync` records its argv and exits without writing,
        so a guard that passes can be observed without touching `/opt/odile`.
        With `real_rsync` and `runtime` it logs the directory it was run from,
        rewrites the hard-coded `/opt/odile/` destination to the fixture
        runtime and executes the real rsync — so the script's own `--dry-run`
        plan (and therefore its source tree) is readable without root.
        """
        (self.shims / "getent").write_text(
            "#!/bin/sh\nexit %d\n" % (0 if getent else 1),
            encoding="utf-8",
        )
        if real_rsync is None:
            body = (
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >> "$DEPLOY_GUARD_RSYNC_LOG"\n'
                f"exit {rsync_status}\n"
            )
        else:
            # Rotate the positional parameters so the destination (the last
            # argument the deployer passes) is first, drop it and append the
            # fixture runtime. No argument is lost and none is re-split.
            body = (
                "#!/bin/sh\n"
                'printf "cwd %s\\n" "$PWD" >> "$DEPLOY_GUARD_RSYNC_LOG"\n'
                'printf "argv %s\\n" "$*" >> "$DEPLOY_GUARD_RSYNC_LOG"\n'
                "n=$#\n"
                "i=0\n"
                'while [ "$i" -lt $((n - 1)) ]; do\n'
                '  first=$1; shift; set -- "$@" "$first"; i=$((i + 1))\n'
                "done\n"
                "shift\n"
                'set -- "$@" "' + str(runtime) + '/"\n'
                'exec "' + str(real_rsync) + '" "$@"\n'
            )
        (self.shims / "rsync").write_text(body, encoding="utf-8")
        for name in ("getent", "rsync"):
            (self.shims / name).chmod(0o755)

    def run(
        self, *args: str, script: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = git_env()
        env["PATH"] = str(self.shims) + os.pathsep + env["PATH"]
        env["DEPLOY_GUARD_RSYNC_LOG"] = str(self.marker)
        return subprocess.run(
            [str(script or self.repo / "scripts/deploy.sh"), *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
        )

    def output(self, result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def refusal_lines(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        return [
            line
            for line in self.output(result).splitlines()
            if "refused" in line
        ]


class GuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-deploy-guard-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def behind_checkout(self) -> tuple[Checkout, dict[str, str]]:
        """A `behind` branch that forked before main's last two commits."""
        checkout = Checkout(self.root)
        checkout.git("branch", "behind")
        missing = advance_main(checkout)
        checkout.git("checkout", "--quiet", "behind")
        return checkout, missing


class RefusalTests(GuardTest):
    def test_a_branch_that_does_not_contain_main_is_refused(self) -> None:
        checkout, missing = self.behind_checkout()
        checkout.shim(getent=False)

        result = checkout.run()
        output = checkout.output(result)
        refusals = checkout.refusal_lines(result)

        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual(len(refusals), 1, output)
        line = refusals[0]
        self.assertIn("HEAD %s" % short(checkout, "HEAD"), line)
        self.assertIn("main %s" % short(checkout, "main"), line)
        self.assertIn("2 commit(s) missing", line)
        self.assertIn(missing["roadmap"], line)
        self.assertIn(missing["plan"], line)
        self.assertIn("ODL-490", line)
        self.assertIn("ODL-334", line)
        # The guard runs before the group check and the rsync: neither the
        # pre-existing failure nor the shim's marker may appear.
        self.assertNotIn("group odile does not exist", output)
        self.assertFalse(checkout.marker.exists(), output)

    def test_the_dry_run_reports_the_same_refusal_and_plans_nothing(self) -> None:
        checkout, missing = self.behind_checkout()
        checkout.shim(getent=False)

        applied = checkout.refusal_lines(checkout.run())
        result = checkout.run("-n")
        output = checkout.output(result)
        dry = checkout.refusal_lines(result)

        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual(dry, applied, output)
        self.assertIn("2 commit(s) missing", dry[0])
        self.assertIn(missing["plan"], dry[0])
        self.assertNotIn("would install", output)
        self.assertFalse(checkout.marker.exists(), output)

    def test_a_branch_that_forked_before_main_moved_is_refused(self) -> None:
        checkout = Checkout(self.root)
        checkout.git("checkout", "--quiet", "-b", "feature")
        checkout.write("feature.txt", "feature work\n")
        checkout.commit("feature work that main does not have")
        missing = advance_main(checkout)
        checkout.git("checkout", "--quiet", "feature")
        checkout.shim(getent=False)

        result = checkout.run()
        output = checkout.output(result)
        refusals = checkout.refusal_lines(result)

        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual(len(refusals), 1, output)
        self.assertIn(missing["roadmap"], refusals[0])
        self.assertIn(missing["plan"], refusals[0])
        # The branch's own commits are not what is missing, so they are not
        # named; only main's absent commits are.
        self.assertNotIn("feature work", refusals[0])
        self.assertFalse(checkout.marker.exists(), output)

    def test_a_detached_head_behind_main_is_refused(self) -> None:
        checkout = Checkout(self.root)
        base = checkout.git("rev-parse", "HEAD").stdout.strip()
        missing = advance_main(checkout)
        checkout.git("checkout", "--quiet", base)
        checkout.shim(getent=False)

        result = checkout.run()
        output = checkout.output(result)
        refusals = checkout.refusal_lines(result)

        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual(len(refusals), 1, output)
        self.assertIn(missing["plan"], refusals[0])
        self.assertFalse(checkout.marker.exists(), output)

    def test_a_checkout_without_a_local_main_branch_is_refused(self) -> None:
        checkout = Checkout(self.root, branch="trunk")
        checkout.shim(getent=False)

        result = checkout.run()
        output = checkout.output(result)
        refusals = checkout.refusal_lines(result)

        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual(len(refusals), 1, output)
        self.assertIn("no local main", refusals[0])
        self.assertNotIn("group odile does not exist", output)
        self.assertFalse(checkout.marker.exists(), output)


class AllowedTests(GuardTest):
    def test_main_passes_the_guard_and_its_dry_run_plans_the_deploy(self) -> None:
        checkout = Checkout(self.root)
        checkout.shim(getent=True)

        result = checkout.run("-n")
        output = checkout.output(result)

        self.assertIn("contains main %s" % short(checkout, "main"), output)
        self.assertNotIn("refused", output)
        self.assertTrue(checkout.marker.exists(), output)
        self.assertIn("--dry-run", checkout.marker.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 1, output)  # the rsync shim's exit

    def test_a_branch_that_contains_main_passes_the_guard(self) -> None:
        checkout = Checkout(self.root)
        checkout.git("checkout", "--quiet", "-b", "feature")
        checkout.write("feature.txt", "feature work\n")
        checkout.commit("feature work on top of main")
        checkout.shim(getent=True)

        result = checkout.run()
        output = checkout.output(result)

        self.assertIn("contains main %s" % short(checkout, "main"), output)
        self.assertNotIn("refused", output)
        self.assertTrue(checkout.marker.exists(), output)

    def test_allow_behind_is_the_documented_rollback_path(self) -> None:
        checkout, _ = self.behind_checkout()
        checkout.shim(getent=True)

        result = checkout.run("--allow-behind")
        output = checkout.output(result)

        self.assertNotIn("refused", output)
        self.assertIn("does not contain main", output)
        self.assertIn("--allow-behind", output)
        self.assertTrue(checkout.marker.exists(), output)


class SourceRootTests(GuardTest):
    """The plan is built from the checkout root, never from `scripts/`.

    Measured 2026-11-14: `cd "$(dirname "$0")"` made `./scripts/deploy.sh`
    rsync the `scripts/` directory over `/opt/odile` — 1305 file deletions,
    including `app.py` and every page under `web/`. These tests run the real
    script and the real rsync, with the destination rewritten to a fixture
    runtime, so the plan itself is the assertion.
    """

    def setUp(self) -> None:
        super().setUp()
        self.rsync = shutil.which("rsync")

    def site_checkout(self) -> Checkout:
        """A checkout with the site files a deploy must not delete."""
        checkout = Checkout(self.root)
        checkout.write("web/index.html", "<!doctype html><title>Odile</title>\n")
        checkout.write("web/verify.html", "<!doctype html><title>Verify</title>\n")
        checkout.write(
            "deploy/seed/deploy_seed.py",
            "import sys\nprint('seed: would converge')\n",
        )
        # The host-local root convenience copy of the entry point exists in
        # this checkout; it must not ship either (see the rsync excludes).
        root_copy = checkout.repo / "deploy.sh"
        root_copy.write_bytes(DEPLOY.read_bytes())
        root_copy.chmod(0o755)
        checkout.commit("site pages, the seed helper and the root deploy copy")
        return checkout

    def runtime_tree(self) -> Path:
        """A fixture `/opt/odile` holding the same site files."""
        runtime = self.root / "runtime"
        (runtime / "web").mkdir(parents=True)
        (runtime / "app.py").write_text("# odile\n", encoding="utf-8")
        (runtime / "web/index.html").write_text(
            "<!doctype html><title>Odile</title>\n", encoding="utf-8"
        )
        (runtime / "web/verify.html").write_text(
            "<!doctype html><title>Verify</title>\n", encoding="utf-8"
        )
        return runtime

    def test_the_plan_source_is_the_checkout_root(self) -> None:
        if self.rsync is None:
            self.skipTest("rsync is required to read the deploy plan")
        checkout = self.site_checkout()
        runtime = self.runtime_tree()
        checkout.shim(getent=True, real_rsync=Path(self.rsync), runtime=runtime)

        result = checkout.run("-n")
        output = checkout.output(result)

        self.assertEqual(result.returncode, 0, output)
        source = next(
            line for line in output.splitlines() if line.startswith("→ source")
        )
        self.assertEqual(
            Path(source.split("  ", 1)[1]).resolve(), checkout.repo.resolve()
        )
        self.assertEqual(
            [line for line in output.splitlines() if line.startswith("*deleting")],
            [],
            output,
        )
        self.assertNotIn("*deleting   app.py", output)
        self.assertNotIn("*deleting   web/", output)
        # Neither entry point is copied: scripts/deploy.sh is excluded by the
        # script's own rsync, and a root-level deploy.sh would be too.
        self.assertNotIn("deploy.sh", output)
        # The rsync ran with the checkout root as its working directory.
        log = checkout.marker.read_text(encoding="utf-8").splitlines()
        self.assertEqual(log[0], "cwd %s" % checkout.repo)

    def test_the_old_script_directory_source_would_delete_the_site(self) -> None:
        """A mutant keeps the test honest: move the source back to scripts/."""
        if self.rsync is None:
            self.skipTest("rsync is required to read the deploy plan")
        checkout = self.site_checkout()
        runtime = self.runtime_tree()
        old = checkout.repo / "scripts/deploy-old.sh"
        text = DEPLOY.read_text(encoding="utf-8")
        mutant = text.replace('cd "$SCRIPT_DIR/.."', 'cd "$SCRIPT_DIR"')
        mutant = mutant.replace("if [ ! -f app.py ]; then", "if false; then")
        self.assertIn('cd "$SCRIPT_DIR"', mutant)
        self.assertIn("if false; then", mutant)
        old.write_text(mutant, encoding="utf-8")
        old.chmod(0o755)
        checkout.shim(getent=True, real_rsync=Path(self.rsync), runtime=runtime)

        result = checkout.run("-n", script=old)
        output = checkout.output(result)

        self.assertIn("→ source  %s/scripts" % checkout.repo, output)
        self.assertIn("*deleting   app.py", output)
        self.assertIn("*deleting   web/verify.html", output)

    def test_a_copy_outside_scripts_refuses_instead_of_planning(self) -> None:
        checkout = Checkout(self.root)
        nested = checkout.repo / "legacy" / "scripts" / "deploy.sh"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(DEPLOY.read_bytes())
        nested.chmod(0o755)
        checkout.shim(getent=False)

        result = checkout.run(script=nested)
        output = checkout.output(result)

        self.assertEqual(result.returncode, 1, output)
        self.assertIn("is not the repository root", output)
        self.assertFalse(checkout.marker.exists(), output)


if __name__ == "__main__":
    unittest.main()
