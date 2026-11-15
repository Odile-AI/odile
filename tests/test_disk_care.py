#!/usr/bin/python3
"""Tests for the disk-care sweeper's /tmp rule (docs/ops/DISK-CARE.md).

A regular file under /tmp of at least 1 GB, older than two hours, with no open
fd is removed and logged with its path, size and mtime; a younger file, or one
a process holds open, is kept. `/tmp` is a tmpfs, so the rule removes outright
and never quarantines.

The tests drive the committed script itself — deploy/disk-care/hermes-disk-care.sh
— in its `--tmp-only` mode over a fixture directory, so a change to the predicate
is a change these tests see. Nothing here reads or writes the real /tmp:
TMP_ROOT, TMP_MIN_BYTES, TMP_STALE_HOURS, TMP_FD_CHECK and LOG are the script's
own overrides, and a 1 GiB fixture is a sparse file, so the suite costs no disk.

DISK_CARE_SH points the suite at a mutant copy of the script; the default is the
committed one. That is how the card proves the suite fails when the age check or
the fd check is removed.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SCRIPT = ROOT / "deploy" / "disk-care" / "hermes-disk-care.sh"
SCRIPT = pathlib.Path(os.environ.get("DISK_CARE_SH") or DEFAULT_SCRIPT)
GIB = 1024**3
PYTHON = "/usr/bin/python3"
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z (?P<message>.*)$")


class SweepFixture(unittest.TestCase):
    """One fixture directory standing in for /tmp, and one log outside it."""

    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="odile-disk-care-"))
        self.root = self.work / "tmp"
        self.root.mkdir()
        self.log = self.work / "sweeper.log"
        self.holders: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for holder in self.holders:
            if holder.stdout is not None:
                holder.stdout.close()
            holder.terminate()
            try:
                holder.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child
                holder.kill()
                holder.wait()
        shutil.rmtree(self.work, ignore_errors=True)

    # --- fixture helpers

    def big_file(self, name: str, size: int = GIB, age_seconds: float = 3 * 3600) -> pathlib.Path:
        """A sparse file of `size` bytes whose mtime is `age_seconds` old."""
        path = self.root / name
        with open(path, "wb") as handle:
            handle.truncate(size)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def hold_open(self, path: pathlib.Path) -> subprocess.Popen[str]:
        """A child that keeps `path` open until tearDown, and has opened it."""
        holder = subprocess.Popen(
            [
                PYTHON,
                "-c",
                "import sys, time\n"
                "handle = open(sys.argv[1], 'rb')\n"
                "print('open', flush=True)\n"
                "time.sleep(120)\n",
                str(path),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.holders.append(holder)
        assert holder.stdout is not None
        self.assertEqual(holder.stdout.readline().strip(), "open")
        return holder

    def stat_meta(self, path: pathlib.Path) -> tuple[int, str]:
        """Exactly what the sweeper logs: `stat -c %s` and `%y` cut at the dot."""
        asked = subprocess.run(
            ["stat", "-c", "%s|%y", str(path)], capture_output=True, text=True, check=True
        )
        size, mtime = asked.stdout.strip().split("|", 1)
        return int(size), mtime.split(".", 1)[0]

    # --- runner

    def sweep(self, dry: bool = False, **overrides: object) -> str:
        env = dict(os.environ)
        env.update(
            {
                "TMP_ROOT": str(self.root),
                "LOG": str(self.log),
                "TMP_MIN_BYTES": str(GIB),
                "TMP_STALE_HOURS": "2",
            }
        )
        env.update({key: str(value) for key, value in overrides.items()})
        command = ["bash", str(SCRIPT), "--tmp-only"]
        if dry:
            command.append("--dry-run")
        done = subprocess.run(command, capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(
            done.returncode, 0, f"sweeper exited {done.returncode}: {done.stderr or done.stdout}"
        )
        return self.log.read_text() if self.log.exists() else ""

    def messages(self, log: str) -> list[str]:
        """The log lines with the UTC timestamp stripped."""
        out = []
        for line in log.splitlines():
            match = LOG_LINE.match(line)
            if match:
                out.append(match.group("message"))
        return out

    def decisions_about(self, log: str, path: pathlib.Path) -> list[str]:
        return [message for message in self.messages(log) if str(path) in message]


class SelectionTest(SweepFixture):
    """≥ 1 GB, > 2 h, no fd: listed by a dry run, unlinked by a real one."""

    def test_dry_run_lists_the_stale_file_and_leaves_it(self) -> None:
        path = self.big_file("image.raw")
        size, mtime = self.stat_meta(path)
        log = self.sweep(dry=True)

        self.assertEqual(
            [message for message in self.messages(log) if str(path) in message],
            [f"would remove {self.root} file {path} ({size} bytes, mtime {mtime})"],
        )
        self.assertTrue(path.exists(), "a dry run must not unlink anything")

    def test_real_run_unlinks_the_stale_file_and_logs_path_size_mtime(self) -> None:
        path = self.big_file("image.raw")
        size, mtime = self.stat_meta(path)
        log = self.sweep()

        self.assertIn(f"rm {self.root} file {path} ({size} bytes, mtime {mtime})", log)
        self.assertFalse(path.exists())
        self.assertIn(f"tmpfs {self.root} used ", log, "the run must end with the tmpfs counter")

    def test_dry_run_and_real_run_agree_on_the_set(self) -> None:
        first = self.big_file("one.raw")
        second = self.big_file("two.raw", size=2 * GIB)
        young = self.big_file("young.raw", age_seconds=60)
        held = self.big_file("held.raw")
        self.hold_open(held)

        dry = self.sweep(dry=True)
        listed = {
            message.split(" file ", 1)[1].rsplit(" (", 1)[0]
            for message in self.messages(dry)
            if message.startswith("would remove ")
        }
        self.assertEqual(listed, {str(first), str(second)}, dry)

        real = self.sweep()
        removed = {
            message.split(" file ", 1)[1].rsplit(" (", 1)[0]
            for message in self.messages(real)
            if message.startswith("rm ")
        }
        self.assertEqual(removed, listed, real)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(young.exists())
        self.assertTrue(held.exists())

    def test_young_file_is_kept(self) -> None:
        path = self.big_file("young.raw", age_seconds=60)
        log = self.sweep()

        self.assertEqual(self.decisions_about(log, path), [], log)
        self.assertTrue(path.exists())

    def test_file_below_the_size_floor_is_kept(self) -> None:
        path = self.big_file("small.raw", size=GIB - 1)
        log = self.sweep()

        self.assertEqual(self.decisions_about(log, path), [], log)
        self.assertTrue(path.exists())

    def test_held_open_file_is_kept(self) -> None:
        path = self.big_file("guest.raw")
        self.hold_open(path)
        size, mtime = self.stat_meta(path)
        log = self.sweep()

        self.assertIn(
            f"keep {self.root} file (held open) {path} ({size} bytes, mtime {mtime})", log
        )
        self.assertTrue(path.exists())

    def test_held_open_file_is_kept_by_the_proc_scan(self) -> None:
        path = self.big_file("guest.raw")
        self.hold_open(path)
        log = self.sweep(TMP_FD_CHECK="proc")

        self.assertIn(f"keep {self.root} file (held open) {path} ", log)
        self.assertTrue(path.exists())


class ThresholdOverrideTest(SweepFixture):
    """TMP_MIN_BYTES and TMP_STALE_HOURS are the documented overrides."""

    def test_byte_and_hour_overrides_change_the_selection(self) -> None:
        eligible = self.big_file("scratch.bin", size=2048, age_seconds=90 * 60)
        young = self.big_file("fresh.bin", size=2048, age_seconds=30 * 60)
        log = self.sweep(TMP_MIN_BYTES="1024", TMP_STALE_HOURS="1")

        self.assertIn(f"rm {self.root} file {eligible} ", log)
        self.assertFalse(eligible.exists())
        self.assertTrue(young.exists())


if __name__ == "__main__":
    unittest.main()
