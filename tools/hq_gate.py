#!/usr/bin/python3
"""Run the test modules that exist only at HQ against *this* tree.

A card worktree is a fresh checkout of its branch, so `unittest discover -s
tests` there sees only tracked modules: measured at `main` `2b4fb01`, 79 test
modules on disk and 64 tracked, and a handoff quoting "1947 tests" was a subset
gate against the merge gate's 2293. The 15 modules that exist only at HQ carry
rules about the *tree* (every `docs/*.md` quoting the UKI `f7bc0ceb…` must name
`t_c1e0f336`; every craft playbook keeps its five sections), so a card that
breaks one is green in its own worktree and red — for everyone — in `main`.

This tool closes the gap. It:

1. reads the files the overlay needs from HQ's own git: every file `git ls-files
   --others --ignored --exclude-standard` names there, minus byte-code caches,
   sibling worktrees and other noise, **and** every file `main` tracks that the
   branch's HEAD does not. Both sets drop any path the branch tracks (the
   branch's bytes win, exactly as in a merge). The second set is ODL-941:
   `main` gained `docs/craft/finance.md` after a card's branch was cut, HQ
   tracks the file so the ignored list cannot name it, and `test_craft` failed
   on a card that had touched no craft document. HQ defaults to this
   repository's primary checkout, resolved from `git rev-parse
   --git-common-dir`, so a card worktree configures nothing;
2. creates a throwaway worktree at the branch's **commit** with `git worktree
   add --detach`, so an uncommitted edit cannot be what is tested;
3. overlays both sets on it, which is what the merged tree in `main` looks
   like: the branch's tracked files plus the private machinery plus whatever
   `main` gained since the branch was cut (bytes read from the commits, so an
   uncommitted edit at HQ changes nothing);
4. runs every HQ-only `tests/test_*.py` module there and reports one line per
   module — the branch's documents in front of HQ's rules;
5. removes the worktree and the copy, so the card's own tree is never touched.

Why the overlay rather than the card worktree directly: a bare copy of the
modules into `tests/` fails 8 of 15 at import or on `docs/backlog.yaml`, because
the HQ-only modules read the private machinery too. Measured 2026-11-20 at
`main` `2b1e60c`: 15/15 pass with the overlay.

Usage:  ./tools/hq_gate.py [--hq DIR] [--tree DIR] [--list] [--keep]
Exit:   0 every HQ-only module passed (or HQ/the modules are absent); 1 a
        module failed; 2 the tree is not a checkout or the worktree could not
        be created.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_TMP = "/var/tmp"
#: Where the throwaway worktree is built: a run copies the HQ-only set (tens of
#: MB) and removes it again, so `ODILE_HQ_GATE_TMP` can move it to a roomier disk.

NOISE_SEGMENTS = frozenset({"__pycache__", ".worktrees", ".fresh-clone", ".venv",
                            "node_modules", ".git"})
NOISE_SUFFIXES = (".pyc", ".pyo")
TESTS_ROOT = "tests/"
RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)


def clean_env() -> dict[str, str]:
    """The caller's environment minus `GIT_*`.

    `.githooks/pre-commit` exports `GIT_DIR`/`GIT_INDEX_FILE` for the outer
    repository; every command here runs with a different `cwd`, and inheriting
    them would make `git ls-tree` read the wrong history.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True,
                          errors="replace", env=clean_env())


def git_bytes(*args: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """`git` for a blob: no text decoding, so a binary file survives."""
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, env=clean_env())


def is_noise(path: str) -> bool:
    """A cache, a sibling worktree or an editor backup is not an HQ-only file."""
    if path.endswith(NOISE_SUFFIXES):
        return True
    return any(part in NOISE_SEGMENTS for part in Path(path).parts)


def main_checkout(tree: Path) -> Path:
    """The repository's primary checkout — where the HQ-only files live.

    `git rev-parse --git-common-dir` answers the shared git directory from any
    linked worktree (`<repo>/.git`), so the primary checkout is its parent. A
    card worktree therefore needs no configured path, and no operator path is
    written into the tree (the public build replaces such a path with
    `<checkout>`).
    """
    result = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=tree)
    common = Path(result.stdout.strip()) if result.stdout.strip() else None
    if result.returncode != 0 or common is None or not common.is_dir():
        raise SystemExit(f"hq-gate: cannot resolve the common git directory in {tree}: "
                         f"{result.stderr.strip()[:160]}")
    return common.parent if common.name == ".git" else common


def tracked_files(tree: Path, revision: str = "HEAD") -> set[str]:
    """Every path the revision's tree carries — by default the branch's `HEAD`."""
    result = git("ls-tree", "-r", "--name-only", "-z", revision, cwd=tree)
    if result.returncode != 0:
        raise SystemExit(f"hq-gate: cannot read {revision} in {tree}: "
                         f"{result.stderr.strip()[:160]}")
    return {path for path in result.stdout.split("\0") if path}


def hq_only_files(hq: Path, tracked: set[str]) -> list[str]:
    """Files HQ's git ignores and the branch does not track, sorted, noise off.

    `--ignored --exclude-standard`: the private machinery is *excluded* at HQ
    (`.git/info/exclude` and `.gitignore`), so `ls-files --others` alone finds
    none of it. A path the branch tracks is dropped even when HQ ignores a file
    at the same name — a merge would keep the branch's.
    """
    result = git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=hq)
    if result.returncode != 0:
        raise SystemExit(f"hq-gate: cannot list the HQ-only files in {hq}: "
                         f"{result.stderr.strip()[:160]}")
    found = [path for path in result.stdout.split("\0") if path]
    return sorted(path for path in found if not is_noise(path) and path not in tracked)


def integration_commit(hq: Path) -> str | None:
    """The commit `main` names in the HQ repository, or `None`.

    The gate has to carry what the merge will carry: the files `main` tracks
    that the branch's HEAD does not. The ref is read from HQ's *git*, not from
    HQ's working tree, because a checkout can sit on another branch (during the
    ODL-941 investigation it did) while its `main` ref is still the integration
    branch. `origin/main` covers a clone that never made a local branch; with no
    `main` at all — a public clone on another default branch — the overlay takes
    nothing from it, which is what a merge there would add too.
    """
    for ref in ("main", "origin/main"):
        result = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=hq)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def main_only_files(hq: Path, tracked: set[str], revision: str | None = None) -> list[str]:
    """Files `main` tracks and the branch's HEAD does not, sorted, noise off.

    A file that exists only on `main` is in neither set the gate used to read:
    HQ *tracks* it, so `ls-files --others --ignored` cannot name it, and the
    branch's HEAD has not got it — ODL-941, where `main` gained
    `docs/craft/finance.md` after the card was cut and `test_craft` failed on a
    card that touched no craft document. A path the branch tracks is dropped
    here too, so the branch's bytes still win.
    """
    if revision is None:
        revision = integration_commit(hq)
    if revision is None:
        return []
    return sorted(path for path in tracked_files(hq, revision)
                  if not is_noise(path) and path not in tracked)


def modules_from(files: list[str]) -> list[str]:
    """The dotted module names among the overlaid files, in reading order."""
    return [
        path[len(TESTS_ROOT):-len(".py")].replace("/", ".")
        for path in files
        if path.startswith(TESTS_ROOT)
        and path.endswith(".py")
        and Path(path).name.startswith("test_")
    ]


def hq_only_modules(hq: Path, tracked: set[str]) -> list[str]:
    """The HQ-only `tests/test_*.py` modules, as dotted module names."""
    return modules_from(hq_only_files(hq, tracked))


def overlay(hq: Path, destination: Path, files: list[str]) -> list[str]:
    """Copy the HQ-only files into the throwaway tree; return the paths copied."""
    copied: list[str] = []
    for relative in files:
        source = hq / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            if target.exists() or target.is_symlink():
                target.unlink()
            os.symlink(os.readlink(source), target)
        elif source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(source, target)
        copied.append(relative)
    return copied


def overlay_commit(hq: Path, revision: str, destination: Path, files: list[str]) -> list[str]:
    """Write `revision`'s bytes for `files` into the throwaway tree; return the paths.

    The bytes come from the commit, not from HQ's working copy: an uncommitted
    edit at HQ is not what the merge carries, and the file need not be in HQ's
    checkout at all when that checkout sits on a branch without it. Git's mode
    travels with the blob, so an executable tool stays executable.
    """
    if not files:
        return []
    listing = git("ls-tree", "-r", "-z", revision, cwd=hq)
    if listing.returncode != 0:
        raise SystemExit(f"hq-gate: cannot read {revision} in {hq}: "
                         f"{listing.stderr.strip()[:160]}")
    entries: dict[str, tuple[str, str]] = {}
    for record in listing.stdout.split("\0"):
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) == 3 and fields[1] == "blob":
            entries[path] = (fields[0], fields[2])
    copied: list[str] = []
    for relative in files:
        entry = entries.get(relative)
        if entry is None:  # a gitlink (submodule) has no blob to copy
            continue
        mode, object_id = entry
        blob = git_bytes("cat-file", "blob", object_id, cwd=hq)
        if blob.returncode != 0:
            raise SystemExit(f"hq-gate: cannot read {relative} at {revision}: "
                             f"{blob.stderr.decode('utf-8', 'replace').strip()[:160]}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            if target.exists() or target.is_symlink():
                target.unlink()
            os.symlink(blob.stdout.decode("utf-8", "surrogateescape"), target)
        else:
            target.write_bytes(blob.stdout)
            os.chmod(target, 0o755 if mode == "100755" else 0o644)
        copied.append(relative)
    return copied


def child_env() -> dict[str, str]:
    env = clean_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def ran_line(output: str) -> str:
    """The module's own `Ran N tests in …` line, or a placeholder."""
    for line in output.splitlines():
        if RAN_RE.match(line):
            return line.strip()
    return ""


def run_modules(tree: Path, modules: list[str]) -> tuple[list[str], int]:
    """Run each module in its own process; return (failed modules, tests run)."""
    failed: list[str] = []
    total = 0
    for module in modules:
        result = subprocess.run([sys.executable, "-m", "unittest", f"tests.{module}"],
                                cwd=str(tree), capture_output=True, text=True,
                                errors="replace", env=child_env())
        output = result.stderr + result.stdout
        match = RAN_RE.search(output)
        count = int(match.group(1)) if match else 0
        total += count
        verdict = "pass" if result.returncode == 0 else "FAIL"
        print(f"{verdict}  {module} ({ran_line(output) or 'no summary line'})")
        if result.returncode != 0:
            failed.append(module)
            lines = [line for line in output.splitlines() if line.strip()]
            # A failing rule often quotes the document it examined in one
            # assertion message; cap the line so the verdict stays readable and
            # the module's own file:line is still in the transcript.
            for line in lines[-25:]:
                print(f"      {line if len(line) <= 400 else line[:400] + ' …'}")
    return failed, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run HQ-only test modules against this checkout's commit.")
    parser.add_argument("--hq", default=os.environ.get("ODILE_HQ") or None,
                        help="the checkout carrying the private files (default: this "
                             "repository's primary checkout, from --git-common-dir)")
    parser.add_argument("--tree", default=os.getcwd(),
                        help="the checkout under test (default: the current directory)")
    parser.add_argument("--list", action="store_true",
                        help="print the files the overlay would carry, then exit")
    parser.add_argument("--keep", action="store_true",
                        help="keep the throwaway tree and print its path (debugging)")
    args = parser.parse_args(argv)

    hq = Path(args.hq).expanduser().resolve() if args.hq else None
    tree = Path(args.tree).expanduser().resolve()

    inside = git("rev-parse", "--is-inside-work-tree", cwd=tree)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        print(f"FAIL  {tree} is not a git work tree")
        return 2

    if hq is None:
        hq = main_checkout(tree)
    if not (hq / ".git").exists():
        print(f"hq-gate: skipped (no HQ checkout at {hq})")
        return 0

    tracked = tracked_files(tree)
    revision = integration_commit(hq)
    hq_files = hq_only_files(hq, tracked)
    main_files = main_only_files(hq, tracked, revision)
    files = sorted(set(hq_files) | set(main_files))
    modules = modules_from(files)

    if args.list:
        for path in files:
            print(path)
        print(f"hq-gate: {len(modules)} HQ-only module(s), {len(files)} file(s)")
        return 0

    if not modules:
        print(f"hq-gate: OK — no HQ-only test module (HQ {hq}, tree {tree})")
        return 0

    print(f"hq-gate: {len(modules)} module(s) to run, {len(files)} file(s) to overlay "
          f"from {hq} ({len(hq_files)} HQ-only, {len(main_files)} only on main)")

    tmp_parent = Path(os.environ.get("ODILE_HQ_GATE_TMP") or DEFAULT_TMP)
    tmp_parent.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="odile-hq-gate-", dir=str(tmp_parent)))
    destination = base / "tree"
    failed: list[str] = []
    total = 0
    try:
        added = git("worktree", "add", "--detach", str(destination), "HEAD", cwd=tree)
        if added.returncode != 0:
            print(f"FAIL  cannot build a throwaway worktree from {tree} HEAD: "
                  f"{(added.stderr or added.stdout).strip()[:200]}")
            return 2
        overlay(hq, destination, hq_files)
        if revision is not None:
            overlay_commit(hq, revision, destination, main_files)
        failed, total = run_modules(destination, modules)
    finally:
        if args.keep:
            print(f"hq-gate: kept {destination}")
        else:
            git("worktree", "remove", "--force", str(destination), cwd=tree)
            shutil.rmtree(base, ignore_errors=True)

    if failed:
        print(f"hq-gate: FAILED — {len(failed)} of {len(modules)} module(s) "
              f"({total} test(s) ran): {', '.join(failed)}")
        return 1
    print(f"hq-gate: OK — {len(modules)} module(s), {total} test(s) ran against "
          f"{tree} HEAD plus {len(files)} overlaid file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
