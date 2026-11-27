#!/usr/bin/python3
"""Tests for ODL-361: one lint over every enabled cron prompt.

The bug was three hand copies of one sentence in three days: ODL-958 retired
"Commit only `docs/backlog.yaml` as `plan: pulse <date>`" from the product
pulse and `docs/product/OPERATING.md:72`, ODL-959 found the identical sentence
in the CoS pulse (job 8798bce1c58c, eight runs a day), and t_dc0690c5 found the
third in the default profile's weekly plan. No tool read the stores together.

These tests use a throwaway store laid out the way Hermes lays one out — a
default `cron/jobs.json` and `profiles/<name>/cron/jobs.json` — so the fixture
is the ODL-959 shape without touching the live stores. A separate test pins the
skip: a store that does not exist is named by path, never reported clean.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "prompt_lint.py"
sys.path.insert(0, str(ROOT / "tools"))

import prompt_lint  # noqa: E402

# ODL-958's exact sentence, quoted in that card's context. The doc and line are
# the retirement record, not a pointer into today's text.
RETIRED_SENTENCE = "Commit only `docs/backlog.yaml` as `plan: pulse <date>`"
RETIRED_DOC = "docs/product/OPERATING.md:72"

# The sentence ODL-959 replaced it with in the CoS pulse: it still mentions
# `plan: pulse`, which is why a lint that greps for that token would red the
# fixed prompt and miss nothing real.
FIXED_PROMPT = (
    "Validate with ./tools/board_sync.py check; if it passes run seed --apply, "
    "release --all --apply. Make **no commit** \u2014 the plan is recorded by "
    "`seed --apply` and the host YAML's sha256, and a `plan: pulse <date>` commit "
    "would be empty because `docs/backlog.yaml` is host-only "
    "(`.git/info/exclude`)."
)


def job(job_id: str, name: str, prompt: str, *, enabled: bool = True) -> dict:
    return {
        "id": job_id,
        "name": name,
        "prompt": prompt,
        "enabled": enabled,
        "schedule": "0 */3 * * *",
    }


class FixtureStore:
    """A temporary Hermes-shaped store: one default and one profile store."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, path: Path, jobs: list[dict]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"jobs": jobs, "updated_at": "2026-11-27T00:00:00+00:00"}),
            encoding="utf-8",
        )
        return path

    def default(self, jobs: list[dict]) -> Path:
        return self.write(self.root / "cron" / "jobs.json", jobs)

    def profile(self, name: str, jobs: list[dict]) -> Path:
        return self.write(self.root / "profiles" / name / "cron" / "jobs.json", jobs)


def run_lint(*stores: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *[str(store) for store in stores]],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


class RetiredListTest(unittest.TestCase):
    def test_the_plan_pulse_sentence_carries_its_doc_and_line(self) -> None:
        rows = {row.phrase: row for row in prompt_lint.RETIRED}
        self.assertIn(RETIRED_SENTENCE, rows)
        self.assertEqual(rows[RETIRED_SENTENCE].doc, RETIRED_DOC)
        self.assertIn("ODL-958", rows[RETIRED_SENTENCE].retired_by)
        self.assertIn("ODL-959", rows[RETIRED_SENTENCE].retired_by)


class StoreTest(unittest.TestCase):
    def test_odl_959_shape_is_red_then_green(self) -> None:
        """The same job: the hand copy fails, the retirement wording passes."""
        with tempfile.TemporaryDirectory() as tmp:
            store = FixtureStore(Path(tmp))

            # Before: the sentence is wrapped across lines, as a 5,219-char
            # prompt wraps it. A raw substring match would miss this copy.
            red_store = store.profile(
                "chief-of-staff",
                [
                    job(
                        "8798bce1c58c",
                        "Odile CoS pulse",
                        "Run ./tools/board_sync.py check, then seed --apply.\n\n"
                        "Commit only\n`docs/backlog.yaml` as\n"
                        "`plan: pulse <date>`.\n",
                    )
                ],
            )
            red = run_lint(red_store)
            self.assertEqual(red.returncode, 1, red.stderr)
            self.assertIn(
                "chief-of-staff 8798bce1c58c Odile CoS pulse \u2014 retired: "
                f"{RETIRED_SENTENCE} ({RETIRED_DOC})",
                red.stdout,
            )
            self.assertIn("1 jobs, 1 retired phrases", red.stdout)

            # After: the ODL-959 wording still says `plan: pulse`, but only to
            # retire the commit, so the job is clean.
            green_store = store.profile(
                "chief-of-staff",
                [job("8798bce1c58c", "Odile CoS pulse", FIXED_PROMPT)],
            )
            green = run_lint(green_store)
            self.assertEqual(green.returncode, 0, green.stderr)
            self.assertIn("1 jobs, 0 retired phrases", green.stdout)
            self.assertNotIn("retired:", green.stdout)

    def test_disabled_job_with_the_sentence_is_not_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FixtureStore(Path(tmp))
            path = store.default(
                [
                    job(
                        "deadbeef",
                        "paused plan",
                        "Commit only `docs/backlog.yaml` as `plan: pulse <date>`.",
                        enabled=False,
                    )
                ]
            )
            result = run_lint(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("0 jobs, 0 retired phrases", result.stdout)

    def test_default_and_profile_stores_are_read_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FixtureStore(Path(tmp))
            default = store.default(
                [job("ff1f2ef7984e", "Odile weekly plan", "No commit.")]
            )
            profile = store.profile(
                "product-lead",
                [
                    job(
                        "b073063c164a",
                        "Odile product pulse",
                        "Commit only `docs/backlog.yaml` as `plan: pulse <date>`.",
                    )
                ],
            )
            result = run_lint(default, profile)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("product-lead b073063c164a Odile product pulse", result.stdout)
            self.assertNotIn("default ff1f2ef7984e", result.stdout)
            self.assertIn("2 jobs, 1 retired phrases", result.stdout)

    def test_the_live_layout_labels_the_default_store_default(self) -> None:
        """A store under `~/.hermes` is the default profile, not a name.

        The live store is `/root/.hermes/cron/jobs.json`, whose owner directory
        is `.hermes`; a fixture rooted anywhere else would label it by the temp
        directory's name and never exercise the branch the live scan prints.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = FixtureStore(Path(tmp) / ".hermes")
            default = store.default(
                [
                    job(
                        "ff1f2ef7984e",
                        "Odile weekly plan",
                        "Commit only `docs/backlog.yaml` as `plan: pulse <date>`.",
                    )
                ]
            )
            profile = store.profile(
                "growth", [job("c0ffee", "Odile growth pulse", "No commit.")]
            )
            result = run_lint(default, profile)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("default ff1f2ef7984e Odile weekly plan", result.stdout)
            self.assertIn("2 jobs, 1 retired phrases", result.stdout)
            self.assertEqual(prompt_lint.profile_name(profile), "growth")

    def test_missing_store_is_a_skip_naming_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "profiles" / "builder" / "cron" / "jobs.json"
            result = run_lint(missing)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"SKIP {missing} \u2014 no cron store", result.stdout)
            self.assertIn("no cron store checked (1 skipped)", result.stdout)
            self.assertNotIn("0 jobs, 0 retired phrases", result.stdout)

    def test_one_present_store_and_one_missing_store_still_names_the_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FixtureStore(Path(tmp))
            present = store.default(
                [job("ff1f2ef7984e", "Odile weekly plan", FIXED_PROMPT)]
            )
            missing = Path(tmp) / "profiles" / "finance" / "cron" / "jobs.json"
            result = run_lint(present, missing)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"SKIP {missing}", result.stdout)
            self.assertIn("1 jobs, 0 retired phrases", result.stdout)

    def test_malformed_store_is_an_error_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cron" / "jobs.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"jobs": "not a list"}', encoding="utf-8")
            result = run_lint(path)
            self.assertEqual(result.returncode, 2)
            self.assertIn(f"ERROR {path}", result.stderr)
            self.assertNotIn("0 retired phrases", result.stdout)


if __name__ == "__main__":
    unittest.main()
