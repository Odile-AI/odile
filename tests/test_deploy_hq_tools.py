#!/usr/bin/python3
"""A deploy installs HQ's private tools, never a worktree's stale copy.

`tools/` is mixed: some files are tracked, but every file `.git/info/exclude`
hides is private machinery that only the HQ checkout holds. `tools/roadmap_page.py`
is the one `odile-roadmap.service` executes, and on 2026-11-23 a deploy from
`.worktrees/t_a0e1fdb5` rsynced that worktree's pre-card copy (a2957af5…) over
the fixed bytes (5f84c3e0…) in `/opt/odile`, so the next tick served the
reverted page. A worktree copy is ignored there too, so `git status` stays
clean and nothing stops it from shipping.

These tests build throwaway git repositories — an HQ checkout with a private
tool, a card worktree that carries a different copy of it, and a fixture runtime
standing in for `/opt/odile` — and run the real scripts with PATH shims for
`getent` and `rsync`. No fixture can reach `/opt/odile` or systemd.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts/deploy.sh"
HQ_TOOLS = ROOT / "scripts/hq_tools.sh"
CHECK = ROOT / "scripts/check_hq_tools.sh"

GIT_IDENTITY = (
    "-c",
    "user.name=Odile HQ Tools",
    "-c",
    "user.email=hq-tools@odile.fyi",
)

HQ_BYTES = b"# HQ roadmap generator\ndef render():\n    return 'fixed bytes'\n"
BACKLOG_BYTES = b"milestones: []\ntasks: []\n"
RUNTIME_BYTES = b"# pre-card generator in /opt/odile\ndef render():\n    return 'old bytes'\n"
WORKTREE_BYTES = b"# a card worktree's stale copy\ndef render():\n    return 'older bytes'\n"


def git_env() -> dict[str, str]:
    """The ambient environment minus every inherited git handle.

    `make check-fast` runs from `.githooks/pre-commit`, which exports `GIT_DIR`
    and `GIT_INDEX_FILE`; a fixture that inherits them commits onto the card's
    branch or reads the wrong ref.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    """An HQ checkout, a card worktree and a runtime under one temp root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.hq = root / "hq"
        self.hq.mkdir()
        self.shims = root / "shims"
        self.shims.mkdir()
        self.log = root / "rsync.log"
        self.runtime = root / "runtime"
        self.real_rsync: Path | None = None
        (self.runtime / "tools").mkdir(parents=True)
        self.git("init", "--quiet", "--initial-branch", "main")
        (self.hq / "app.py").write_text("# Odile\n", encoding="utf-8")
        scripts = self.hq / "scripts"
        scripts.mkdir()
        for source in (DEPLOY, HQ_TOOLS, CHECK):
            target = scripts / source.name
            target.write_bytes(source.read_bytes())
            target.chmod(0o755)
        seed = self.hq / "deploy" / "seed"
        seed.mkdir(parents=True)
        (seed / "deploy_seed.py").write_text(
            "import sys\nprint('seed: would converge')\n", encoding="utf-8"
        )
        tools = self.hq / "tools"
        tools.mkdir()
        (tools / "leak_scan.py").write_text("# tracked machinery\n", encoding="utf-8")
        (tools / "roadmap_page.py").write_bytes(HQ_BYTES)
        cache = tools / "__pycache__"
        cache.mkdir()
        (cache / "roadmap_page.cpython-314.pyc").write_bytes(b"\0bytecode")
        # The roadmap unit reads --backlog /opt/odile/docs/backlog.yaml, and the
        # plan is HQ-only too; a worktree cannot carry it.
        docs = self.hq / "docs"
        docs.mkdir()
        (docs / "backlog.yaml").write_bytes(BACKLOG_BYTES)
        exclude = self.hq / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(
            "# HQ-only paths\n"
            "/tools/roadmap_page.py\n"
            "/tools/__pycache__/\n"
            "/docs/backlog.yaml\n",
            encoding="utf-8",
        )
        self.commit("HQ tree")
        self.worktree = root / "worktree"
        self.git(
            "worktree", "add", "--quiet", "--detach", str(self.worktree), "main"
        )
        # The card's worktree carries its own copy of the private tool. It is
        # ignored there too, so it never appears in `git status`.
        (self.worktree / "tools").mkdir(exist_ok=True)
        (self.worktree / "tools" / "roadmap_page.py").write_bytes(WORKTREE_BYTES)
        # The runtime starts with the copy a previous deploy installed.
        (self.runtime / "tools" / "roadmap_page.py").write_bytes(RUNTIME_BYTES)
        self.shim(getent=True, real_rsync=None)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.hq,
            env=git_env(),
            check=True,
            capture_output=True,
            text=True,
        )

    def commit(self, message: str) -> None:
        self.git("add", "--all")
        self.git(
            *GIT_IDENTITY, "commit", "--quiet", "--no-gpg-sign", "-m", message
        )

    def shim(self, *, getent: bool, real_rsync: Path | None) -> None:
        """Put stand-ins for `getent` and `rsync` on PATH.

        Without `real_rsync`, `rsync` records its argv and exits 0. With it,
        the shim drops rsync's `--dry-run`/`--itemize-changes` flags and
        rewrites the transfer destination to the fixture runtime, so the real
        rsync performs the transfer `deploy.sh` planned without root and
        without touching `/opt/odile`.
        """
        getent_shim = self.shims / "getent"
        getent_shim.write_text(
            "#!/bin/sh\nexit %d\n" % (0 if getent else 1), encoding="utf-8"
        )
        getent_shim.chmod(0o755)
        self.real_rsync = real_rsync
        rsync_shim = self.shims / "rsync"
        if real_rsync is None:
            rsync_shim.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >> "$HQ_TOOLS_RSYNC_LOG"\n'
                "exit 0\n",
                encoding="utf-8",
            )
        else:
            rsync_shim.write_text(
                "#!/usr/bin/python3\n"
                "import os, sys\n"
                "log = os.environ['HQ_TOOLS_RSYNC_LOG']\n"
                "runtime = os.environ['HQ_TOOLS_RUNTIME']\n"
                "with open(log, 'a', encoding='utf-8') as handle:\n"
                "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
                "rsync = os.environ['HQ_TOOLS_REAL_RSYNC']\n"
                "args = [a for a in sys.argv[1:]\n"
                "        if a not in ('--dry-run', '--itemize-changes')]\n"
                "args[-1] = runtime + '/'\n"
                "os.execv(rsync, [rsync] + args)\n",
                encoding="utf-8",
            )
        rsync_shim.chmod(0o755)

    def env(self) -> dict[str, str]:
        env = git_env()
        env["PATH"] = str(self.shims) + os.pathsep + env["PATH"]
        env["HQ_TOOLS_RSYNC_LOG"] = str(self.log)
        env["HQ_TOOLS_RUNTIME"] = str(self.runtime)
        if self.real_rsync is not None:
            env["HQ_TOOLS_REAL_RSYNC"] = str(self.real_rsync)
        return env

    def run(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), *args],
            cwd=self.hq,
            env=self.env(),
            capture_output=True,
            text=True,
        )

    def hq_tools(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run(self.hq / "scripts/hq_tools.sh", *args)

    def deploy(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.worktree / "scripts/deploy.sh"), *args],
            cwd=self.worktree,
            env=self.env(),
            capture_output=True,
            text=True,
        )

    def rsync_argv(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


class FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-hq-tools-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(Path(self._tmp.name))


class ListTests(FixtureTest):
    def test_list_names_every_hidden_path_and_not_the_tracked_one(self) -> None:
        result = self.fixture.hq_tools("list", str(self.fixture.hq))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["docs/backlog.yaml", "tools/roadmap_page.py"],
        )

    def test_list_ignores_bytecode_caches(self) -> None:
        result = self.fixture.hq_tools("list", str(self.fixture.hq))

        self.assertNotIn("__pycache__", result.stdout)
        self.assertNotIn(".pyc", result.stdout)


class InstallTests(FixtureTest):
    def test_install_replaces_the_runtime_copy_with_the_hq_bytes(self) -> None:
        fixture = self.fixture
        result = fixture.hq_tools("install", str(fixture.hq), str(fixture.runtime))

        self.assertEqual(result.returncode, 0, result.stderr)
        deployed = fixture.runtime / "tools" / "roadmap_page.py"
        self.assertEqual(sha256(deployed), sha256(fixture.hq / "tools/roadmap_page.py"))
        self.assertEqual(deployed.read_bytes(), HQ_BYTES)
        self.assertNotEqual(deployed.read_bytes(), WORKTREE_BYTES)
        self.assertIn("installed tools/roadmap_page.py", result.stdout)
        # The roadmap unit's --backlog is HQ-only too; a worktree cannot carry
        # it, and a deploy used to delete it with rsync --delete.
        budget = fixture.runtime / "docs" / "backlog.yaml"
        self.assertEqual(budget.read_bytes(), BACKLOG_BYTES)
        self.assertIn("installed docs/backlog.yaml", result.stdout)
        self.assertFalse((fixture.worktree / "docs" / "backlog.yaml").exists())

    def test_install_is_idempotent_and_says_same(self) -> None:
        fixture = self.fixture
        fixture.hq_tools("install", str(fixture.hq), str(fixture.runtime))
        deployed = fixture.runtime / "tools" / "roadmap_page.py"
        before = deployed.stat().st_mtime_ns

        result = fixture.hq_tools("install", str(fixture.hq), str(fixture.runtime))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("same tools/roadmap_page.py", result.stdout)
        self.assertEqual(deployed.stat().st_mtime_ns, before)

    def test_dry_run_prints_the_plan_and_writes_nothing(self) -> None:
        fixture = self.fixture
        deployed = fixture.runtime / "tools" / "roadmap_page.py"

        result = fixture.hq_tools(
            "install", "--dry-run", str(fixture.hq), str(fixture.runtime)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would install tools/roadmap_page.py", result.stdout)
        self.assertEqual(deployed.read_bytes(), RUNTIME_BYTES)

    def test_install_keeps_the_hq_mode(self) -> None:
        fixture = self.fixture
        (fixture.hq / "tools" / "roadmap_page.py").chmod(0o640)

        fixture.hq_tools("install", str(fixture.hq), str(fixture.runtime))

        deployed = fixture.runtime / "tools" / "roadmap_page.py"
        self.assertEqual(deployed.stat().st_mode & 0o777, 0o640)


class CheckTests(FixtureTest):
    def test_check_passes_on_a_converged_runtime(self) -> None:
        fixture = self.fixture
        fixture.hq_tools("install", str(fixture.hq), str(fixture.runtime))

        result = fixture.hq_tools("check", str(fixture.runtime), str(fixture.hq))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("same tools/roadmap_page.py", result.stdout)

    def test_check_fails_loudly_on_a_stale_runtime_copy(self) -> None:
        fixture = self.fixture

        result = fixture.hq_tools("check", str(fixture.runtime), str(fixture.hq))

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("DIFF tools/roadmap_page.py", result.stdout)


class DeployPlanTests(FixtureTest):
    def test_the_plan_excludes_every_hq_only_tool_and_names_the_hq_install(self) -> None:
        fixture = self.fixture
        fixture.shim(getent=True, real_rsync=None)

        result = fixture.deploy("-n")
        output = result.stdout + result.stderr
        argv = fixture.rsync_argv()

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("--exclude /tools/roadmap_page.py", argv)
        self.assertIn("--exclude /docs/backlog.yaml", argv)
        self.assertNotIn("--exclude /tools/leak_scan.py", argv)
        self.assertIn("would install tools/roadmap_page.py", output)
        self.assertIn("would install docs/backlog.yaml", output)
        self.assertEqual(
            (fixture.worktree / "tools" / "roadmap_page.py").read_bytes(),
            WORKTREE_BYTES,
        )

    def test_a_fixture_deploy_keeps_the_hq_copy_in_the_runtime(self) -> None:
        """Replay the deploy's two writes against a fixture runtime.

        `deploy.sh -n` plans the transfer; the rsync shim executes it (flags
        dropped, destination rewritten) so the worktree's stale copy would land
        if it were not excluded. The install step is then the exact command
        deploy.sh runs after the rsync.
        """
        fixture = self.fixture
        fixture.shim(getent=True, real_rsync=Path(shutil.which("rsync") or "rsync"))

        result = fixture.deploy("-n")
        output = result.stdout + result.stderr
        deployed = fixture.runtime / "tools" / "roadmap_page.py"

        self.assertEqual(result.returncode, 0, output)
        # The rsync kept the runtime's pre-deploy copy: the worktree's stale
        # bytes were excluded, which is the fix.
        self.assertEqual(deployed.read_bytes(), RUNTIME_BYTES)
        # deploy.sh's post-rsync step converges the tool to HQ's bytes.
        install = fixture.hq_tools("install", str(fixture.hq), str(fixture.runtime))
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertEqual(sha256(deployed), sha256(fixture.hq / "tools/roadmap_page.py"))
        self.assertEqual(deployed.read_bytes(), HQ_BYTES)
        self.assertNotEqual(deployed.read_bytes(), WORKTREE_BYTES)
        self.assertEqual(
            (fixture.runtime / "docs" / "backlog.yaml").read_bytes(), BACKLOG_BYTES
        )


GENERATOR_STUB = """#!/usr/bin/python3
import sys
args = sys.argv[1:]
now = args[args.index("--now") + 1]
source = args[args.index("--source-sha") + 1]
sys.stdout.write('<p class="foot">generated %s · source %s · odile.fyi</p>\\n' % (now, source))
"""

GENERATED = "2026-11-23T18:57:28Z"
SOURCE = "abc1234"


def epoch(instant: str) -> int:
    stamp = datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return int(stamp.timestamp())


class LiveCheckTests(unittest.TestCase):
    """scripts/check_hq_tools.sh fails when the tick predates the install.

    The verifier's pass is only worth what its failure is worth, so each guard
    the script carries gets a fixture: a page older than the installed tool, a
    deployed path that is not HQ's bytes, and the converged pass.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-hq-check-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.hq = root / "hq"
        self.hq.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch", "main"],
            cwd=self.hq, env=git_env(), check=True, capture_output=True,
        )
        (self.hq / "app.py").write_text("# Odile\n", encoding="utf-8")
        (self.hq / "tools").mkdir()
        (self.hq / "tools" / "roadmap_page.py").write_text(
            GENERATOR_STUB, encoding="utf-8"
        )
        (self.hq / "docs").mkdir()
        (self.hq / "docs" / "backlog.yaml").write_text("tasks: []\n", encoding="utf-8")
        (self.hq / ".git" / "info" / "exclude").write_text(
            "/tools/roadmap_page.py\n/docs/backlog.yaml\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", *GIT_IDENTITY, "add", "--all"],
            cwd=self.hq, env=git_env(), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", *GIT_IDENTITY, "commit", "--quiet", "--no-gpg-sign", "-m", "hq"],
            cwd=self.hq, env=git_env(), check=True, capture_output=True,
        )
        self.dest = root / "dest"
        (self.dest / "tools").mkdir(parents=True)
        (self.dest / "web").mkdir()
        (self.dest / "web" / "roadmap.html").write_text("<html></html>\n", encoding="utf-8")
        (self.dest / "docs" / "architecture").mkdir(parents=True)
        (self.dest / "docs" / "architecture" / "ADR-upgrades-and-model.md").write_text(
            "# ADR\n", encoding="utf-8"
        )
        install = subprocess.run(
            [str(HQ_TOOLS), "install", str(self.hq), str(self.dest)],
            env=git_env(), capture_output=True, text=True,
        )
        self.assertEqual(install.returncode, 0, install.stderr)
        self.page = root / "roadmap.html"
        self.page.write_text(
            '<p class="foot">generated %s · source %s · odile.fyi</p>\n'
            % (GENERATED, SOURCE),
            encoding="utf-8",
        )
        self.source = root / "roadmap-source"
        self.source.write_text(SOURCE + "\n", encoding="utf-8")

    def run_check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CHECK), str(self.dest), str(self.hq), str(self.page), str(self.source)],
            env=git_env(), capture_output=True, text=True,
        )

    def install_tool_at(self, instant: str) -> None:
        tool = self.dest / "tools" / "roadmap_page.py"
        os.utime(tool, (epoch(instant), epoch(instant)))

    def test_live_check_passes_when_the_page_follows_the_installed_tool(self) -> None:
        self.install_tool_at("2026-11-23T18:00:00Z")

        result = self.run_check()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check-hq-tools: OK", result.stdout)

    def test_live_check_fails_when_the_page_predates_the_installed_tool(self) -> None:
        self.install_tool_at("2026-11-23T19:00:00Z")

        result = self.run_check()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("before", result.stderr or result.stdout)

    def test_live_check_fails_when_a_deployed_path_is_not_hq_bytes(self) -> None:
        (self.dest / "tools" / "roadmap_page.py").write_text(
            "# stale\n", encoding="utf-8"
        )

        result = self.run_check()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("DIFF tools/roadmap_page.py", result.stdout)


if __name__ == "__main__":
    unittest.main()