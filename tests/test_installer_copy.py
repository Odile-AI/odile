#!/usr/bin/python3
"""The installer's copy of the guest export program (ODL-218 follow-up).

`odilectl/offboard.py` owns `EXPORT_PROGRAM`: the bytes `odilectl export
<slug> --script` prints, the program the tenant runs to take its data with it.
The installer is one printable, signed file, so it carries a pinned copy and
`ensure_export_program` places it at `/usr/local/bin/odile-export`.

These tests load the installer as a module and check the copy against its
owner, the generator, the pin, the placement and a one-byte edit. Hermetic: a
temporary root, no network, no tenant.

    /usr/bin/python3 -m unittest tests.test_installer_copy -v
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import offboard  # noqa: E402  (the program's one owner)

INSTALLER = ROOT / "installer" / "odile_install.py"
GENERATOR = ROOT / "installer" / "embed_export.py"


def load(path: Path, name: str):
    """Load one script as a module; dataclasses need it in sys.modules first."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class InstallerExportProgramTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = load(INSTALLER, "odile_install_copy_under_test")
        cls.generator = load(GENERATOR, "embed_export_under_test")
        cls.program = offboard.EXPORT_PROGRAM
        cls.bytes = cls.program.encode("utf-8")
        cls.digest = hashlib.sha256(cls.bytes).hexdigest()

    def scratch(self, tmp: str, port: int):
        return self.installer.Layout.scratch(
            root=Path(tmp), user=pwd.getpwuid(os.geteuid()).pw_name,
            webapp_port=port, public_url=f"http://copy.test:{port}",
        )

    def test_the_embedded_copy_is_the_offboard_program_byte_for_byte(self):
        self.assertEqual(self.installer.EXPORT_PROGRAM_SOURCE, self.program)
        self.assertEqual(self.installer.EXPORT_PROGRAM_SHA256, self.digest)

    def test_the_generator_renders_exactly_the_block_the_installer_carries(self):
        block = self.generator.render_block(self.bytes)
        self.assertIn(block, INSTALLER.read_text(encoding="utf-8"))

    def test_the_copy_is_what_odilectl_export_script_prints(self):
        result = subprocess.run(
            [sys.executable, "-m", "odilectl", "export", "odl-copy-test", "--script"],
            cwd=ROOT, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stdout, self.bytes)

    def test_the_placement_writes_0755_and_a_second_run_reads_same(self):
        with tempfile.TemporaryDirectory(prefix="odile-export-copy-") as tmp:
            layout = self.scratch(tmp, 9148)
            self.assertTrue(self.installer.ensure_export_program(layout, False))
            target = Path(tmp) / "usr/local/bin/odile-export"
            self.assertTrue(target.is_file())
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual(target.read_bytes(), self.bytes)
            self.assertFalse(self.installer.ensure_export_program(layout, False))

    def test_a_one_byte_edit_to_the_copy_is_refused_before_anything_is_written(self):
        with tempfile.TemporaryDirectory(prefix="odile-export-edit-") as tmp:
            layout = self.scratch(tmp, 9149)
            original = self.installer.EXPORT_PROGRAM_SOURCE
            try:
                self.installer.EXPORT_PROGRAM_SOURCE = original.replace(
                    "#!/bin/sh", "#!/bin/sg", 1)
                with self.assertRaises(SystemExit):
                    self.installer.ensure_export_program(layout, False)
            finally:
                self.installer.EXPORT_PROGRAM_SOURCE = original
            self.assertFalse((Path(tmp) / "usr/local/bin/odile-export").exists())

    def test_the_manifest_names_the_program_it_places(self):
        result = subprocess.run([sys.executable, str(INSTALLER), "--manifest"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["export_program_sha256"], self.digest)


if __name__ == "__main__":
    unittest.main()
