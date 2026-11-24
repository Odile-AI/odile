"""The public repository carries the product tree, not the operating record.

The repository is published on every merge: an ordinary commit IS a
publication. The record of what belongs there is the exclusion list in
`tools/build_public_tree.py` and the approved decision in
`docs/product/DECISION-ci-publication-gate.md` — "the operating record is
local-only". Both are HQ-host files, so this fence carries the same list as
data and reads the tracked tree with `git ls-files`, which is exactly what a
push publishes.

Why the fence exists: between 2026-11-14 and 2026-11-21 the 31 became tracked
by ordinary commits — 28 operating-record files (four upskill commits and
`d27c1d7`) and three test modules (`cf05e31`, `424ef3d`, `9e4374d`) — and every
merge pushed them. They were not scrubbed, so the public repository served the HQ
IPv4, the Servury box and account ids, a provider IPv6 prefix, a tenant
instance id and this checkout's path, and 19 `leak-allow.tsv` rows had been
added to waive them at the gate. `t_91f138b3` removed 29 of the 31 on
2026-11-21; this test is what makes the removal stick.

Two modules are tracked although the cut drops them. They run and pass in the
published tree (they are the tests `.github/workflows/check.yml` pins a
container for), and what they cannot survive is the cut's scrub, not
publication: `tests/test_sealed_image.py`'s pin test reads a scrubbed pin.
They are named below so the fence cannot "fix" them by accident.
"""

from __future__ import annotations

import fnmatch
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Built from two halves so no file in the tree carries the tool's full name:
# a published file naming it is exactly what this fence forbids.
_MERGE = "merge" + "_card"

# The local-only roots and files, from `tools/build_public_tree.py`'s EXCLUDE
# and the `.git/info/exclude` patterns it mirrors.
LOCAL_ONLY_GLOBS = (
    "docs/ops/*",
    "docs/craft/*",
    "docs/growth/*",
    "docs/brand/*",
    "docs/evidence/*",
    "*evidence/*",
    "prototype/*",
    "spike/*",
    "deploy/backup/*",
    "deploy/image/out/*",
    "*.mp4",
    "kms_ed25519_facts.json",
    "docs/product/DECISION-*",
    "docs/product/REVIEW-*",
    "docs/product/RECUT-*",
    "docs/product/REESTIMATE-*",
    "docs/product/SPIKE-*",
    "docs/product/PRFAQ-*",
)
LOCAL_ONLY_FILES = (
    "AGENTS.md",
    "deploy.sh",
    "docs/AGENT-ORG.md",
    "docs/MVP-V0.1.md",
    "docs/RESTORE.md",
    "docs/backlog.yaml",
    "docs/product/OPERATING.md",
    "docs/product/ROADMAP-PAGE-CEO.md",
    "docs/runbooks/backup-hq.md",
    "docs/runbooks/testing-host.md",
)
# `docs/product/` is mixed: the builder's INCLUDE names three files and the
# rest of the directory is the operating record (PR/FAQ, growth queue, decision
# and review memos). A positive list is the only form that catches a new file.
PRODUCT_PUBLIC = (
    "docs/product/POSITIONING.md",
    "docs/product/FIRST-RUN-SIX-ACTS.md",
    "docs/product/CLAIMS-RECUT-STRICT.md",
)
# HQ machinery: the board, the pulse, the merge gate and the suites that drive
# them. A published copy only ever skips.
LOCAL_ONLY_TOOLS = (
    "tools/build_public_tree.py",
    "tools/roadmap_page.py",
    "tools/board_sync.py",
    "tools/card_acceptance.py",
    "tools/handoff_check.py",
    "tools/pipeline_stats.py",
    "tools/product_pulse.py",
    "tools/brief_data.py",
    "tools/spend.py",
    "tools/go_live_watch.py",
    "tools/integrate_done.py",
    f"tools/{_MERGE}.py",
    "tools/backup_hq.py",
    "scripts/check_testing_host.sh",
)
LOCAL_ONLY_TESTS = (
    f"tests/test_{_MERGE}.py",
    "tests/test_board_sync.py",
    "tests/test_card_acceptance.py",
    "tests/test_brief_data.py",
    "tests/test_pipeline_tools.py",
    "tests/test_product_pulse.py",
    "tests/test_spend.py",
    "tests/test_go_live_watch.py",
    "tests/test_craft.py",
    "tests/test_backup_hq.py",
    "tests/test_testing_host.py",
    "tests/test_web_icon.py",
    "tests/test_roadmap_page.py",
    "tests/test_claim_worker.py",
    "tests/test_purchase_route.py",
    "tests/test_record_corrections.py",
)
# Tracked, and dropped from the cut on purpose: the cut scrubs, they need the
# unscrubbed bytes. Both pass in the published tree.
CUT_EXCLUDED_BUT_PUBLIC = (
    "tests/test_installer_copy.py",
    "tests/test_sealed_image.py",
)


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        check=True, capture_output=True,
    ).stdout
    return [p.decode() for p in out.split(b"\0") if p]


class PublicTreeShapeTests(unittest.TestCase):
    def test_no_tracked_path_is_local_only(self) -> None:
        allowed = set(CUT_EXCLUDED_BUT_PUBLIC) | set(LOCAL_ONLY_TOOLS) | set(
            LOCAL_ONLY_TESTS
        )
        offenders = []
        for path in tracked():
            if path in allowed:
                continue
            if path.startswith("docs/product/") and path not in PRODUCT_PUBLIC:
                offenders.append(path)
            elif path in LOCAL_ONLY_FILES or any(
                fnmatch.fnmatch(path, glob) for glob in LOCAL_ONLY_GLOBS
            ):
                offenders.append(path)
        self.assertEqual(
            offenders, [],
            "an ordinary commit publishes these; they belong to the operating "
            "record, not the product tree",
        )

    def test_no_tracked_file_names_the_hq_merge_tool(self) -> None:
        # Built from two halves so this file does not itself trip the search.
        tool = _MERGE + ".py"
        out = subprocess.run(
            ["git", "-C", str(REPO), "grep", "-l", "-F", "-e", tool],
            capture_output=True, text=True,
        )
        self.assertEqual(
            out.returncode, 1,
            "a published file names the HQ-only merge tool a reader cannot "
            f"open:\n{out.stdout}",
        )


if __name__ == "__main__":
    unittest.main()
