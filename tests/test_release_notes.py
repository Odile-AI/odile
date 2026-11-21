#!/usr/bin/python3
"""The signed release document must name the artifact the cut ships.

t_e23f3288's round-4 cut (`2026.11.19-7`) said `first_visit.py is main's
ODL-579 file, b0cac695...` while the installer shipped `b36cebe8...` — main's
pre-merge copy named in the document that names the cut, next to an
`install.md` §12.6 that named the shipped file, so the two documents cut
together disagreed about the artifact. A note that mentions a file has to name
the bytes this cut carries; naming the other side of a merge is not enough.

    /usr/bin/python3 -m unittest tests.test_release_notes -v
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "release" / "release.json"
FIRST_VISIT = ROOT / "installer" / "first_visit.py"


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(RELEASE.read_text(encoding="utf-8"))
        self.notes = self.document["notes"]

    def test_the_notes_name_the_first_visit_file_this_cut_ships(self) -> None:
        shipped = hashlib.sha256(FIRST_VISIT.read_bytes()).hexdigest()[:8]
        if "first_visit.py" not in self.notes:
            self.skipTest("this cut's note does not mention first_visit.py")
        self.assertIn(
            shipped, self.notes,
            "release.json's note mentions first_visit.py but does not name the "
            f"file this cut ships ({shipped}…); the document and the "
            "installer disagree about the artifact",
        )


if __name__ == "__main__":
    unittest.main()
