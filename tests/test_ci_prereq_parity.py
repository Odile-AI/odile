#!/usr/bin/python3
"""A binary `prereqs` requires is a package both CI gate jobs install.

`Makefile`'s `prereqs` target names the host binaries `make check` cannot run
without. CI's two gate jobs (`check`, `fresh-check`) each install their own
toolchain from an apt list. The rule that the two lists carry every prereq was
prose in the workflow header until this module, and the class drifted three
times: `ss` (iproute2, ODL-944), a host write (ODL-350), and `sshpass`
(ODL-360 — red on every push from run 45762410714 until 45795255292, while
every local gate was green because HQ had the binary).

These tests derive the binary names from the gate's own source of truth — the
`command -v` lines in the `prereqs` recipe — resolve each to the package that
ships it, and assert that package is installed by *both* gate jobs. The map
lives here, not in the workflow, because a list scanned against itself can
never notice a missing entry: a binary nobody mapped is a question, not a pass,
so it fails closed and names this file.

`shellcheck` is the one prereq installed by a SHA-256-pinned tarball rather
than apt; that counts as installed, and a job that stops verifying and
installing it is red.

The workflow is a parameter of `audit()`, so the same assertion is run against
the pre-fix file from the red run (`git show 82fb9ba:…`) and must fail there.
A guard that passes on the broken file is a change detector.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"

# The jobs that run `make check`. `demo-check`, `publish-lint` and
# `history-secrets` run other targets and their own minimal toolchains.
GATE_JOBS = ("check", "fresh-check")

# binary -> the apt package that ships it. Declared, never derived from the
# workflow: an unknown binary must fail closed, not pass because the workflow
# happens to install a package of the same name.
APT_PACKAGES = {
    "python3": "python3",
    "openssl": "openssl",
    "ssh-keygen": "openssh-client",
    "ssh-keyscan": "openssh-client",
    "sshpass": "sshpass",
}

# binary -> installed by a checksum-verified download into /usr/local/bin.
TARBALLED = {"shellcheck"}

THIS_FILE = "tests/test_ci_prereq_parity.py"

# The commit of the red run (45762410714): `prereqs` named sshpass and neither
# apt list carried it. Named here so the discrimination test says what it read.
PRE_FIX_COMMIT = "82fb9ba"

ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:::=|::=|:=|\?=|\+=|=)\s*(.+?)\s*$")
VARIABLE_RE = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
COMMAND_V_RE = re.compile(r"command\s+-v\s+(\"[^\"]+\"|'[^']+'|\S+)")


def job_block(workflow: str, name: str) -> str:
    """The job's own YAML lines, from its key to the next job."""
    lines = workflow.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.rstrip() == f"  {name}:":
            start = index
            break
    if start is None:
        raise AssertionError(f"the workflow has no {name!r} job")
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            break
        block.append(line)
    return "\n".join(block)


def recipe_lines(makefile: str, target: str) -> list[tuple[int, str]]:
    """(line number, text) for the target's recipe.

    Make comments and blank lines between recipe lines do not end a rule — the
    `prereqs` target carries a paragraph about the OpenSSH binaries in the
    middle of its recipe — so the walk skips them and only a new non-recipe
    line ends the block.
    """
    lines = makefile.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if line.startswith(target + ":")), None)
    if start is None:
        raise AssertionError(f"the Makefile has no {target!r} rule")
    body: list[tuple[int, str]] = []
    for offset in range(start + 1, len(lines)):
        line = lines[offset]
        if line.startswith("\t"):
            body.append((offset + 1, line))
        elif not line.strip() or line.lstrip().startswith("#"):
            continue
        else:
            break
    return body


def variables(makefile: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in makefile.splitlines():
        match = ASSIGN_RE.match(line)
        if match:
            found.setdefault(match.group(1), match.group(2))
    return found


def expand(token: str, table: dict[str, str]) -> str:
    """Substitute `$(NAME)`/`${NAME}` from the Makefile's own assignments."""
    for _ in range(5):
        expanded = VARIABLE_RE.sub(
            lambda match: table.get(match.group(1) or match.group(2), match.group(0)),
            token)
        if expanded == token:
            break
        token = expanded
    return token


def prereq_binaries(makefile: str) -> list[tuple[str, int, str]]:
    """(binary, line, raw token) for every `command -v` in `prereqs`.

    The recipe's variables (`$(PYTHON)`, `$(SHELLCHECK)`) are resolved from
    the Makefile, so the interpreter is `python3`, not a make expression.
    """
    table = variables(makefile)
    seen: set[str] = set()
    found: list[tuple[str, int, str]] = []
    for number, line in recipe_lines(makefile, "prereqs"):
        for match in COMMAND_V_RE.finditer(line):
            raw = match.group(1)
            resolved = expand(raw.strip("\"'"), table)
            binary = Path(resolved).name
            if binary in seen:
                continue
            seen.add(binary)
            found.append((binary, number, raw))
    return found


def apt_packages(block: str) -> set[str]:
    """Every package an `apt-get … install` line in this job names."""
    packages: set[str] = set()
    command = ""
    for line in block.splitlines():
        stripped = line.strip()
        if not command and not stripped.startswith("apt-get"):
            continue
        command = f"{command} {stripped}".strip() if command else stripped
        if command.endswith("\\"):
            command = command[:-1].rstrip()
            continue
        words = command.split()
        if "install" in words:
            packages.update(word for word in words[words.index("install") + 1:]
                            if not word.startswith("-"))
        command = ""
    return packages


def installs_by_tarball(block: str, binary: str) -> bool:
    """The job downloads the binary, verifies a SHA-256 and installs it on PATH."""
    return ("sha256sum -c -" in block
            and f"/usr/local/bin/{binary}" in block
            and re.search(r"(?:curl|wget)\b", block) is not None)


def audit(workflow: str, makefile: str | None = None) -> list[str]:
    """Findings: prereq binaries neither gate job installs. Empty is green."""
    source = MAKEFILE.read_text(encoding="utf-8") if makefile is None else makefile
    blocks = {job: job_block(workflow, job) for job in GATE_JOBS}
    findings: list[str] = []
    for binary, line, token in prereq_binaries(source):
        where = f"Makefile:{line} (prereqs: command -v {token})"
        if binary in APT_PACKAGES:
            package = APT_PACKAGES[binary]
            for job in GATE_JOBS:
                if package not in apt_packages(blocks[job]):
                    findings.append(
                        f"{where}: {binary} needs package {package!r}, and the "
                        f"{job!r} job does not apt-get install it")
        elif binary in TARBALLED:
            for job in GATE_JOBS:
                if not installs_by_tarball(blocks[job], binary):
                    findings.append(
                        f"{where}: {binary} has no SHA-256-verified "
                        f"/usr/local/bin/{binary} install in the {job!r} job")
        else:
            findings.append(
                f"{where}: {binary!r} has no declared package — add it to "
                f"APT_PACKAGES or TARBALLED in {THIS_FILE}")
    return findings


def historical_workflow(sha: str) -> str | None:
    """The workflow at `sha`, or None when this clone does not carry it."""
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", "show",
         f"{sha}:.github/workflows/check.yml"],
        cwd=ROOT, text=True, capture_output=True)
    return result.stdout if result.returncode == 0 else None


class BothGateJobsInstallEveryPrereq(unittest.TestCase):
    def test_every_prereq_is_installed_by_both_gate_jobs(self) -> None:
        findings = audit(WORKFLOW.read_text(encoding="utf-8"),
                         MAKEFILE.read_text(encoding="utf-8"))
        self.assertEqual(findings, [], "\n".join(findings))

    def test_the_parse_reads_prereqs_and_resolves_its_variables(self) -> None:
        binaries = prereq_binaries(MAKEFILE.read_text(encoding="utf-8"))
        self.assertTrue(binaries, "the prereqs recipe names no binary")
        for binary, line, token in binaries:
            with self.subTest(binary=binary):
                self.assertNotIn("$(", binary,
                                 f"Makefile:{line} names {token} and the test could not resolve it")
                self.assertRegex(binary, r"^[A-Za-z0-9._+-]+$")

    def test_every_prereq_is_a_strict_check(self) -> None:
        # A warning would let CI run on without the binary; `prereqs` exits
        # non-zero, and that is the behaviour the workflow must match.
        checked = 0
        for line, text in recipe_lines(MAKEFILE.read_text(encoding="utf-8"), "prereqs"):
            if "command -v" in text:
                checked += 1
                with self.subTest(line=line):
                    self.assertRegex(text, r"\|\|\s*\{.*exit 1;?\s*\}", text)
        self.assertGreaterEqual(checked, 1, "the prereqs recipe names no binary")


class TheGuardDiscriminates(unittest.TestCase):
    """The same audit is red on the pre-fix workflow and green at the head."""

    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_the_pre_fix_apt_lists_are_red_for_both_jobs(self) -> None:
        # The exactly pre-fix shape: both apt lists without sshpass.
        broken = self.workflow.replace("openssh-client sshpass gnupg",
                                       "openssh-client gnupg")
        self.assertEqual(broken.count("openssh-client gnupg"),
                         self.workflow.count("openssh-client sshpass gnupg"),
                         "the fixture no longer removes every sshpass apt entry")
        findings = audit(broken)
        self.assertEqual(len(findings), 2, findings)
        for job in GATE_JOBS:
            self.assertTrue(
                any("sshpass" in finding and f"{job!r} job" in finding
                    for finding in findings),
                f"no finding names the {job} job: {findings}")

    def test_one_job_is_not_enough(self) -> None:
        # The first occurrence is `check`; `fresh-check` keeps sshpass.
        broken = self.workflow.replace("openssh-client sshpass gnupg",
                                       "openssh-client gnupg", 1)
        findings = audit(broken)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("'check' job", findings[0])
        self.assertNotIn("'fresh-check' job", findings[0])

    def test_a_job_that_stops_verifying_the_tarball_is_red(self) -> None:
        head, tail = self.workflow.split("\n  fresh-check:", 1)
        broken_tail = "\n".join(line for line in tail.splitlines()
                                if "sha256sum -c -" not in line)
        findings = audit(f"{head}\n  fresh-check:{broken_tail}")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("shellcheck", findings[0])
        self.assertIn("'fresh-check' job", findings[0])

    def test_the_workflow_at_the_red_run_is_red(self) -> None:
        text = historical_workflow(PRE_FIX_COMMIT)
        if text is None:
            self.skipTest(f"commit {PRE_FIX_COMMIT} is not in this clone "
                          "(a shallow CI checkout); the current-file tests above still run")
        findings = audit(text)
        for job in GATE_JOBS:
            self.assertTrue(
                any("sshpass" in finding and f"{job!r} job" in finding
                    for finding in findings),
                f"the audit passed the {job} job at {PRE_FIX_COMMIT}: {findings}")

    def test_an_unmapped_prereq_fails_closed(self) -> None:
        # A new `command -v` line in `prereqs` alone must be red, naming the
        # file to edit — not a silent pass because the workflow happens to
        # install a package by that name.
        text = MAKEFILE.read_text(encoding="utf-8").replace(
            '\t@command -v sshpass >/dev/null 2>&1',
            '\t@command -v rsync >/dev/null 2>&1 || { echo "make check: '
            'rsync is not installed (apt-get install rsync)" >&2; exit 1; }\n'
            '\t@command -v sshpass >/dev/null 2>&1')
        self.assertNotEqual(text, MAKEFILE.read_text(encoding="utf-8"))
        findings = audit(self.workflow, text)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("'rsync'", findings[0])
        self.assertIn(THIS_FILE, findings[0])


if __name__ == "__main__":
    unittest.main()
