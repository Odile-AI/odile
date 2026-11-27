#!/usr/bin/python3
"""Prompt lint — every enabled cron prompt against the retired-sentence list.

A rule retired from a document does not retire itself from the job prompts that
copied it by hand. ODL-958 retired "Commit only `docs/backlog.yaml` as
`plan: pulse <date>`" from `docs/product/OPERATING.md:72`; ODL-959 found the
same sentence in the CoS pulse (job 8798bce1c58c, eight runs a day), and
t_dc0690c5 found the third copy in the default profile's weekly plan. Each card
fixed one copy because no tool read the stores together.

This lint is that tool. It reads the default store and every profile store on
this host, keeps the jobs with ``enabled: true``, and fails naming the profile,
job id, job name, sentence and the document it was retired from. A store that
does not exist is skipped by path, never counted as clean.

Usage:
    ./tools/prompt_lint.py                  # every store this host has
    ./tools/prompt_lint.py PATH [PATH ...]  # only the stores named (fixtures)
    ./tools/prompt_lint.py --list           # the retired sentences and where

Exit codes: 0 clean or nothing to check, 1 a retired sentence survives in an
enabled prompt, 2 a store exists but cannot be read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERMES_HOME = Path("/root/.hermes")
DEFAULT_STORE = HERMES_HOME / "cron" / "jobs.json"

EM_DASH = "\u2014"


@dataclass(frozen=True)
class Retired:
    """One sentence that a document retired, and where it was retired from.

    Add a row when a rule change retires a sentence from a prompt. The match is
    over normalised whitespace and case, so a prompt wrapped at a different
    column still matches; backticks and punctuation stay significant.
    """

    phrase: str
    doc: str
    retired_by: str


# The one list every enabled prompt is checked against. Keep the doc and line
# as the retiring commit recorded them, not as the document reads today: the
# point is to find a copy of the sentence that was retired there.
RETIRED = (
    Retired(
        phrase="Commit only `docs/backlog.yaml` as `plan: pulse <date>`",
        doc="docs/product/OPERATING.md:72",
        retired_by="ODL-958 (the CoS pulse, job 8798bce1c58c, was ODL-959; "
        "the default weekly plan was t_dc0690c5)",
    ),
)


def normalise(text: str) -> str:
    """The comparison space: one line, one case, punctuation intact."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def default_stores() -> list[Path]:
    """The default store plus every profile store, in a stable order."""
    return [DEFAULT_STORE, *sorted(HERMES_HOME.glob("profiles/*/cron/jobs.json"))]


def profile_name(path: Path) -> str:
    """The profile a store belongs to, by the store's own layout.

    `/root/.hermes/cron/jobs.json` is the default profile; a store under
    `/root/.hermes/profiles/<name>/cron/` is `<name>`. A fixture may copy that
    layout, so the label comes from the path rather than from a hardcoded map.
    """
    if path.name == "jobs.json" and path.parent.name == "cron":
        owner = path.parent.parent
        if owner.name == ".hermes":
            return "default"
        return owner.name
    return path.parent.name


def load_jobs(path: Path) -> list[dict]:
    """The jobs list from one store; a malformed store is a hard error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError(f"{path}: no 'jobs' list")
    return [job for job in jobs if isinstance(job, dict)]


def scan_store(path: Path) -> tuple[int, list[str]]:
    """One read of one store: its enabled count and every retired sentence."""
    jobs = load_jobs(path)
    enabled = [job for job in jobs if job.get("enabled") is True]
    findings: list[str] = []
    for job in enabled:
        prompt = normalise(str(job.get("prompt") or ""))
        for retired in RETIRED:
            if normalise(retired.phrase) not in prompt:
                continue
            job_id = str(job.get("id") or "(no id)")
            job_name = str(job.get("name") or "(no name)")
            findings.append(
                f"{profile_name(path)} {job_id} {job_name} {EM_DASH} retired: "
                f"{retired.phrase} ({retired.doc})"
            )
    return len(enabled), findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when an enabled cron prompt still carries a retired sentence."
    )
    parser.add_argument(
        "stores",
        nargs="*",
        type=Path,
        help="cron store paths; default: /root/.hermes and every profile",
    )
    parser.add_argument(
        "--list", action="store_true", help="print the retired sentences and exit"
    )
    args = parser.parse_args(argv)

    if args.list:
        for retired in RETIRED:
            print(f"{retired.phrase} ({retired.doc}; retired by {retired.retired_by})")
        return 0

    stores = args.stores or default_stores()
    if not stores:
        print("0 jobs, 0 retired phrases")
        return 0

    checked = 0
    skipped = 0
    findings: list[str] = []
    for path in stores:
        if not path.is_file():
            print(f"SKIP {path} {EM_DASH} no cron store")
            skipped += 1
            continue
        try:
            store_jobs, store_findings = scan_store(path)
        except ValueError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        checked += store_jobs
        findings.extend(store_findings)

    for line in findings:
        print(line)

    if checked == 0 and skipped:
        print(f"no cron store checked ({skipped} skipped)")
        return 0

    print(f"{checked} jobs, {len(findings)} retired phrases")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
