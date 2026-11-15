#!/usr/bin/python3
"""The forbid class must run, and it must fail closed; the allowlist must read true.

`tools/leak_scan.py` answers whether a tree names a real machine: an address, a
mailbox, a home path, or a literal from the list of vendor ids and account ids
the operator must never publish. That last class is only as good as its list. On
2026-11-15 the class was dead twice over: the built tree carried no
`leak-forbid.txt`, so the default never resolved, and a `--forbid-file` naming a
file that did not exist was a silent PASS. A missing list looked exactly like a
clean tree.

These tests pin the two behaviours the drill depends on: a planted forbidden
literal fails and its removal passes, and a named list that does not exist is a
finding and exit 2. They also pin the smaller pieces that keep the class honest:
the list is not scanned for its own literals, an absent default is printed (never
a silent skip), and a malformed list row is a finding.

The second half pins the allowlist's own record, which is the other way a gate
goes quiet: a `constant` row expires `never`, a `debt` row carries the date of
the card that clears it and fails past it, a row whose reason contains its own
value is a finding, and the count of waived credentials is printed on every run.
The header's format line and the scanner's docstring must name the same fields,
because they are the two places a reader learns the format.

Every test runs the real scanner against a throwaway git repository, so the
`git ls-files` walk and the exit codes are the ones the gate uses.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER = ROOT / "tools" / "leak_scan.py"
ALLOWLIST = ROOT / "leak-allow.tsv"
LABEL = "probe-box"
LITERAL = "deadbeefcafe"
# Assembled from parts so the scanner's own tree does not carry the probe address:
# a test that plants a leak must not itself be a leak, or the gate it tests fails.
PROBE_IP = ".".join(["45", "33", "32", "156"])
PROBE_IP_2 = ".".join(["45", "33", "32", "157"])


class ForbidClass(unittest.TestCase):
    """A git repo with one clean file; each test adds the list and the probe."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "leak-allow.tsv").write_text(
            "# no exceptions in this fixture\n", encoding="utf-8")
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), "--tree", *args],
            cwd=self.repo, capture_output=True, text=True, timeout=120)

    def commit(self, message: str = "fixture") -> None:
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=probe@example.test", "-c", "user.name=probe",
             "commit", "-qm", message],
            cwd=self.repo, check=True)

    def write_list(self) -> None:
        (self.repo / "leak-forbid.txt").write_text(
            f"# label<TAB>regex\n{LABEL}\t{LITERAL}\n", encoding="utf-8")

    def test_a_named_list_that_does_not_exist_exits_2(self) -> None:
        result = self.scan("--forbid-file", "missing-forbid.txt")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FINDING allow-file", result.stdout)
        self.assertIn("missing-forbid.txt", result.stdout)

    def test_a_planted_forbidden_literal_fails_then_passes(self) -> None:
        self.write_list()
        probe = self.repo / "probe.txt"
        probe.write_text(f"vendor box {LITERAL}\n", encoding="utf-8")
        failed = self.scan()
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn("FINDING forbidden-value", failed.stdout)
        self.assertIn(f"({LABEL})", failed.stdout)
        probe.unlink()
        passed = self.scan()
        self.assertEqual(passed.returncode, 0, passed.stdout)
        self.assertIn("PASS: nothing to leak", passed.stdout)

    def test_the_list_itself_is_not_a_finding(self) -> None:
        # The list carries the literals by definition; if the walk scanned it,
        # every label would be its own finding and the gate could never pass.
        self.write_list()
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"forbid list:     leak-forbid.txt (1 labels)", result.stdout)

    def test_an_absent_default_is_printed_not_silent(self) -> None:
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("absent (leak-forbid.txt)", result.stdout)
        self.assertIn("forbidden-value class did not run", result.stdout)

    def test_a_malformed_list_row_is_a_finding(self) -> None:
        (self.repo / "leak-forbid.txt").write_text(
            f"# label<TAB>regex\n{LABEL} no-tab-here\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbid-file", result.stdout)
        self.assertIn("expected label<TAB>regex", result.stdout)

    def test_history_scans_the_probe_but_not_the_list(self) -> None:
        # The published repository carries the list from its first commit, so
        # history mode must skip the list by path or every publish re-finds it.
        self.write_list()
        (self.repo / "probe.txt").write_text(f"vendor box {LITERAL}\n", encoding="utf-8")
        self.commit()
        result = subprocess.run(
            [sys.executable, str(SCANNER), "--history"],
            cwd=self.repo, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stdout)
        findings = [line for line in result.stdout.splitlines()
                    if line.startswith("FINDING forbidden-value")]
        self.assertEqual(len(findings), 1, result.stdout)
        self.assertIn("probe.txt", findings[0])


class AllowlistRecord(unittest.TestCase):
    """The allowlist is a record: its format, its classes, its reasons, its count."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "leak-allow.tsv").write_text("# no exceptions in this fixture\n",
                                                   encoding="utf-8")
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), "--tree"],
            cwd=self.repo, capture_output=True, text=True, timeout=120)

    def write_probe(self, *values: str) -> None:
        (self.repo / "probe.txt").write_text(
            "".join(f"host {value}\n" for value in values), encoding="utf-8")

    def write_allow(self, *rows: str) -> None:
        (self.repo / "leak-allow.tsv").write_text(
            "# fixture allowlist\n" + "".join(rows), encoding="utf-8")

    @staticmethod
    def row(value: str, *, klass: str = "constant", credential: str = "no",
            reason: str = "a fixture value", expires: str = "never",
            rule: str = "public-ip", path: str = "*") -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return "\t".join([rule, path, f"sha256:{digest}", klass, credential,
                          reason, expires]) + "\n"

    def test_an_expired_debt_row_is_a_finding(self) -> None:
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, klass="debt", expires="2026-01-01"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING stale-allow", result.stdout)
        self.assertIn("expired 2026-01-01", result.stdout)

    def test_a_live_debt_row_passes(self) -> None:
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, klass="debt", expires="2027-06-30"))
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("allowlisted:    1", result.stdout)

    def test_a_constant_row_expires_never_and_passes(self) -> None:
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP))
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("allowlisted:    1", result.stdout)

    def test_a_constant_row_with_a_date_is_a_format_finding(self) -> None:
        # A date on a constant re-creates the single cliff the class column removed.
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, expires="2027-12-31"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING allow-file", result.stdout)
        self.assertIn("a constant row expires never", result.stdout)

    def test_a_debt_row_cannot_carry_never(self) -> None:
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, klass="debt", expires="never"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("needs the date of the card that clears it", result.stdout)

    def test_the_old_five_field_row_is_rejected(self) -> None:
        self.write_probe(PROBE_IP)
        digest = hashlib.sha256(PROBE_IP.encode("utf-8")).hexdigest()[:12]
        self.write_allow("\t".join(["public-ip", "*", f"sha256:{digest}",
                                     "a fixture value", "2027-12-31"]) + "\n")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("expected 7 tab-separated fields", result.stdout)

    def test_a_reason_containing_its_own_value_is_a_finding(self) -> None:
        # Rows 17 and 18 of the live allowlist did this: the row matched the
        # reason text that described it, so "every row matched" proved nothing.
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, reason=f"the probe address {PROBE_IP} must be rejected"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING self-satisfying", result.stdout)
        self.assertIn("reason contains its own value", result.stdout)

    def test_the_summary_counts_rows_credentials_and_debt(self) -> None:
        self.write_probe(PROBE_IP, PROBE_IP_2)
        self.write_allow(self.row(PROBE_IP, credential="yes"),
                         self.row(PROBE_IP_2))
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("allowlist:       2 rows, 1 credentials, 0 debt", result.stdout)

    def test_the_header_and_the_docstring_name_the_same_format(self) -> None:
        header = next(line for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
                      if line.lstrip("# ").startswith("rule "))
        docstring = next(line for line in SCANNER.read_text(encoding="utf-8").splitlines()
                         if "<TAB>path-glob" in line)
        self.assertEqual("".join(header.lstrip("#").split()),
                         "".join(docstring.split()))
        self.assertIn("<TAB>class<TAB>credential<TAB>reason<TAB>expires",
                      "".join(header.split()))

    def test_the_live_allowlist_loads_with_no_format_finding(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("leak_scan", SCANNER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["leak_scan"] = module  # dataclasses resolves its own module by name
        spec.loader.exec_module(module)
        rows, problems = module.load_allow(ROOT)
        self.assertEqual(problems, [])
        self.assertEqual(sum(1 for row in rows if row.credential == "yes"), 2)


if __name__ == "__main__":
    unittest.main()
