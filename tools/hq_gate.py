#!/usr/bin/python3
"""Run the test modules that exist only at HQ against *this* tree.

A card worktree is a fresh checkout of its branch, so `unittest discover -s
tests` there sees only tracked modules: measured at `main` `2b4fb01`, 79 test
modules on disk and 64 tracked, and a handoff quoting "1947 tests" was a subset
gate against the merge gate's 2293 (82 against 66 at `main` `ef23edf`, 2026-11-20).
The modules that exist only at HQ carry rules about the *tree* (every `docs/*.md`
quoting the UKI `f7bc0ceb…` must name `t_c1e0f336`; every craft playbook keeps
its five sections), so a card that breaks one is green in its own worktree and
red — for everyone — in `main`.

This tool closes the gap. It:

1. reads the files the overlay needs from HQ's own git: every file `git ls-files
   --others --ignored --exclude-standard` names there, minus byte-code caches,
   sibling worktrees and other noise, **and** `main`'s side of the merge — the
   paths the branch left alone, taken as `main` has them. Both sets drop any
   path the branch tracks (the branch's bytes win, exactly as in a merge). The
   second set is ODL-941 and ODL-479: `main` gained `docs/craft/finance.md`
   after a card's branch was cut, and later repaired a citation in
   `docs/runbooks/recycle.md` the same branch never touched — HQ *tracks* both,
   so the ignored list cannot name them, and the gate ran a tree the merge would
   never produce. A third set is a seat's staged batch: a path `git add`ed at HQ
   and not committed yet is in neither of the others (the index is not "others",
   and `main` has not got it), so it is read from HQ's index (`git cat-file blob
   :<path>`, with the index's mode). Measured 2026-11-21: a staged
   `docs/craft/security.md` reded `test_craft` on every card for hours, on the
   branch and on `main`, because the preview carried the rule and not the
   playbook. HQ defaults to this repository's primary checkout, resolved
   from `git rev-parse --git-common-dir`, so a card worktree configures
   nothing;
2. creates a throwaway worktree at the branch's **commit** with `git worktree
   add --detach`, so an uncommitted edit cannot be what is tested;
3. overlays the sets on it, which is what the merged tree in `main` looks
   like: the branch's tracked files plus the private machinery plus main's side
   of the merge (bytes read from the commits, so an uncommitted edit at HQ
   changes nothing), plus the seat's staged batch read from HQ's index. A path
   the branch left alone takes `main`'s bytes; a path
   both sides changed gets the merge's own text (`git merge-file`, three-way),
   and a real conflict keeps the branch's bytes; a path the branch deleted stays
   deleted; and a path `main` deleted while the branch left it at the base bytes
   is removed from the preview, so the gate cannot run — or red on — a document
   the merge will never produce (the ODL-941 shape in the deletion direction).
   Unless HQ's own checkout still carries that path: the private machinery is
   *ignored*, so `main` `7ce0f2b` deleted `docs/craft/*.md` from the tree while
   HQ's disk kept them, and the preview must keep HQ's bytes too — the merge
   deletes the tree file, HQ's ignore rules put a working copy back — and drop
   only the index entry, so `git ls-files` answers what HQ's checkout does. A
   path HQ no longer has keeps the deletion. The overlay is also *staged*, so a
   rule that asks git what the tree tracks (`git ls-files`) reads the merge's
   index rather than the branch's.
   `card_acceptance` owns the clean-merge question;
4. runs every `tests/test_*.py` module among those files there and reports one
   line per module — the branch's documents in front of HQ's rules;
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


def ignored_files(hq: Path) -> list[str]:
    """Every file HQ's git ignores, sorted, noise off — the private machinery.

    `--ignored --exclude-standard`: the private machinery is *excluded* at HQ
    (`.git/info/exclude` and `.gitignore`), so `ls-files --others` alone finds
    none of it. This is the unfiltered set; `hq_only_files` drops the paths the
    branch tracks.
    """
    result = git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=hq)
    if result.returncode != 0:
        raise SystemExit(f"hq-gate: cannot list the HQ-only files in {hq}: "
                         f"{result.stderr.strip()[:160]}")
    found = [path for path in result.stdout.split("\0") if path]
    return sorted(path for path in found if not is_noise(path))


def hq_only_files(hq: Path, tracked: set[str]) -> list[str]:
    """Files HQ's git ignores and the branch does not track, sorted, noise off.

    A path the branch tracks is dropped even when HQ ignores a file at the same
    name — a merge would keep the branch's. The exception is a path `main`
    deleted while the branch left it: the merge deletes it, but HQ's checkout
    keeps the ignored bytes, and `hq_retained_files` names that set.
    """
    return [path for path in ignored_files(hq) if path not in tracked]


def staged_added_files(hq: Path) -> list[str]:
    """Every path staged at HQ and absent from its HEAD, sorted, noise off.

    `git diff --cached --diff-filter=A` names a path added to HQ's index that
    HQ's HEAD does not carry: `git add`ed but not committed. It is in neither
    set the gate read before — `ls-files --others` skips an index path, and
    `main` has no such path to give bytes from.
    """
    result = git("diff", "--cached", "--name-only", "--diff-filter=A", "-z", cwd=hq)
    if result.returncode != 0:
        raise SystemExit(f"hq-gate: cannot list the staged files in {hq}: "
                         f"{result.stderr.strip()[:160]}")
    found = [path for path in result.stdout.split("\0") if path]
    return sorted(path for path in found if not is_noise(path))


def staged_files(hq: Path, tracked: set[str], carried: set[str] | None = None) -> list[str]:
    """Files staged at HQ, filtered like the other overlay sources.

    A staged path is the seat's uncommitted batch: an HQ-only rule that reads it
    (a seat stages a new `docs/craft/<seat>.md` and `test_craft`'s `SEATS`
    already names it) reded on a tree with the rule and no playbook. Measured
    2026-11-21: every card's `make check-hq` failed for the hours the file sat
    staged, and the card-merge tool refused every merge in the same window.

    Noise is dropped, a path the branch tracks keeps the branch's bytes, and a
    path the merge takes from `main` (`carried`) keeps main's — a commit, not an
    index entry, is what merges.
    """
    skip = set(tracked) | set(carried or ())
    return [path for path in staged_added_files(hq) if path not in skip]


def hq_retained_files(deleted: list[str], ignored: list[str],
                      staged: list[str]) -> tuple[list[str], list[str]]:
    """The paths `main` deleted that HQ still carries — (on disk, in its index).

    `deleted_paths` names a path `main` removed while the branch left the base
    bytes, and the preview must usually remove it (the ODL-941 shape in the
    deletion direction). At HQ the path can survive anyway: the private
    machinery is *ignored*, so when `main` `7ce0f2b` deleted `docs/craft/*.md`
    (2026-11-21) the files stayed on HQ's disk and every rule that reads them
    ran green there while the preview removed them and reded `test_craft` on a
    card that touched none of them. A path HQ still carries — ignored on disk,
    or staged in its index — is what HQ's own checkout has after the merge, so
    the preview keeps HQ's bytes and main's deletion loses to them.
    """
    deleted_set = set(deleted)
    kept_ignored = [path for path in ignored if path in deleted_set]
    kept_staged = [path for path in staged if path in deleted_set]
    return kept_ignored, kept_staged


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


def tree_entries(repo: Path, revision: str) -> dict[str, tuple[str, str]]:
    """`{path: (mode, blob)}` for every blob in `revision` — a tree, not a working copy."""
    result = git_bytes("ls-tree", "-r", "-z", "--full-tree", revision, cwd=repo)
    if result.returncode != 0:
        raise SystemExit(f"hq-gate: cannot read {revision} in {repo}: "
                         f"{result.stderr.decode('utf-8', 'replace').strip()[:160]}")
    entries: dict[str, tuple[str, str]] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        fields = meta.decode("ascii", "replace").split()
        if len(fields) == 3 and fields[1] == "blob":
            entries[path.decode("utf-8", "surrogateescape")] = (fields[0], fields[2])
    return entries


def merge_base(tree: Path, revision: str) -> str | None:
    """The commit the branch and `revision` last shared, or None across two repositories.

    A fixture HQ from another repository shares no history, and then nothing is
    known about what either side touched: every path `main` has and the branch
    lacks is carried, and a path the branch tracks keeps the branch's bytes.
    """
    result = git("merge-base", "HEAD", revision, cwd=tree)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def carried_paths(main: dict[str, tuple[str, str]], base: dict[str, tuple[str, str]],
                  branch: dict[str, tuple[str, str]]) -> list[str]:
    """The paths the merge takes from `main` because the branch left them alone.

    Three-way, path by path: `main` added it and the branch never had it → it
    arrives; the branch still carries the base's bytes and `main` moved → main's
    arrive (the repaired `docs/runbooks/recycle.md` citation reaches every card
    cut before the repair); the branch changed it → the branch's bytes stay,
    and if `main` changed it too `both_changed_paths` names it for the
    three-way text merge; the branch deleted it → it stays deleted, as a merge
    would.
    """
    carried: list[str] = []
    for path, entry in main.items():
        branch_entry = branch.get(path)
        if branch_entry is None:
            if path not in base:
                carried.append(path)
        elif branch_entry == base.get(path) and branch_entry != entry:
            carried.append(path)
    return sorted(carried)


def deleted_paths(main: dict[str, tuple[str, str]], base: dict[str, tuple[str, str]],
                  branch: dict[str, tuple[str, str]]) -> list[str]:
    """The paths the merge deletes because `main` deleted them.

    The mirror of `carried_paths`, and three-way for the same reason: `main`
    lacks the path; if the branch still carries the base's bytes, the merge
    deletes it and so must the preview. A path the branch changed survives (a
    modify/delete conflict, which `card_acceptance` reports); a path the branch
    deleted is already absent, so it needs no second removal. With no shared
    history (`base` empty) nothing is known to have been deleted on either side.
    """
    deleted: list[str] = []
    for path, entry in base.items():
        if path in main:
            continue
        branch_entry = branch.get(path)
        if branch_entry is not None and branch_entry == entry:
            deleted.append(path)
    return sorted(deleted)


def both_changed_paths(main: dict[str, tuple[str, str]], base: dict[str, tuple[str, str]],
                       branch: dict[str, tuple[str, str]]) -> list[str]:
    """The paths both sides changed: a merge combines the two edits, not one side's.

    `carried_paths` takes `main`'s bytes for a path the branch left at the
    base's, and a path only the branch changed keeps the branch's. A path *both*
    sides changed is neither: a merge applies main's hunks to the branch's text,
    and keeping the branch's whole file reds the preview on a tree nobody's
    merge produces — measured 2026-11-20 on ODL-479, where `main` added a
    `.githooks/pre-push` entry to the `Makefile` after the card was cut while
    the branch added `check-hq` to the same file: `test_forbid_gate` and
    `test_make_scope` (main's newer tests, arriving by `carried_paths`) failed
    in the preview while `git merge-tree --write-tree main <branch>` was clean.
    """
    return sorted(
        path for path, entry in main.items()
        if path in base and path in branch
        and entry != base[path] and branch[path] != base[path]
    )


def combine_paths(destination: Path, tree: Path, hq: Path,
                  base: dict[str, tuple[str, str]], main: dict[str, tuple[str, str]],
                  files: list[str]) -> tuple[list[str], list[str]]:
    """Write the merge's own text for `files`; return (merged, conflicted).

    `git merge-file` is the three-way text merge a real merge runs, so the
    preview gets the merge's answer: main's hunks and the branch's hunks
    together. On a real conflict the branch's bytes stay — `card_acceptance`
    reports the conflict, and the gate must not invent either side. The same
    applies to a binary file or a symlink, which have no text merge.
    """
    merged: list[str] = []
    conflicted: list[str] = []
    with tempfile.TemporaryDirectory(prefix="merge-", dir=str(destination.parent)) as scratch_dir:
        scratch = Path(scratch_dir)
        for index, relative in enumerate(files):
            target = destination / relative
            if not target.is_file() or target.is_symlink():
                conflicted.append(relative)
                continue
            base_entry = base.get(relative)
            main_entry = main.get(relative)
            if base_entry is None or main_entry is None or "120000" in (base_entry[0], main_entry[0]):
                conflicted.append(relative)
                continue
            ours = target.read_bytes()
            theirs = blob_bytes(hq, main_entry[1], f"{relative} at main")
            original = blob_bytes(tree, base_entry[1], f"{relative} at the merge base")
            if b"\0" in ours or b"\0" in theirs or b"\0" in original:
                conflicted.append(relative)
                continue
            ours_path, base_path, theirs_path = (scratch / f"{index}.{name}"
                                                for name in ("ours", "base", "theirs"))
            ours_path.write_bytes(ours)
            base_path.write_bytes(original)
            theirs_path.write_bytes(theirs)
            result = subprocess.run(
                ("git", "merge-file", "-p", "-L", "branch", "-L", "base", "-L", "main",
                 str(ours_path), str(base_path), str(theirs_path)),
                cwd=str(scratch), capture_output=True, env=clean_env(),
            )
            if result.returncode == 0:
                target.write_bytes(result.stdout)
                merged.append(relative)
            else:
                conflicted.append(relative)
    return merged, conflicted


def blob_bytes(repo: Path, object_id: str, what: str) -> bytes:
    """The bytes of `object_id` in `repo`'s object store."""
    result = git_bytes("cat-file", "blob", object_id, cwd=repo)
    if result.returncode != 0:
        raise SystemExit(f"hq-gate: cannot read {what}: "
                         f"{result.stderr.decode('utf-8', 'replace').strip()[:160]}")
    return result.stdout


def main_deleted_files(hq: Path, tree: Path, revision: str | None = None) -> list[str]:
    """Paths the preview must remove: `main` deleted them, the branch left them.

    Without this set the throwaway worktree keeps a file the merge would delete,
    so an HQ-only rule that requires it gone reds on a tree nobody's merge
    produces — the ODL-941 shape in the deletion direction. Noise is dropped
    for the same reason as the overlay: a cache is not a merge decision.
    """
    if revision is None:
        revision = integration_commit(hq)
    if revision is None:
        return []
    shared = merge_base(tree, revision)
    if shared is None:
        return []
    return [path for path in deleted_paths(
        tree_entries(hq, revision),
        tree_entries(tree, shared),
        tree_entries(tree, "HEAD"),
    ) if not is_noise(path)]


def stage_overlay(destination: Path, files: list[str]) -> None:
    """Make the preview's index the merge's, so `git ls-files` answers the preview.

    The preview is a worktree at the branch's commit: main's carried files sit
    in it untracked and main's deletions stay in the index. A module that asks
    git what the tree tracks therefore reads the *branch's* answer — measured
    2026-11-20 on ODL-479, where main's `test_make_scope` pins the lint scope to
    `git ls-files -- '*.sh'` and the preview's index did not carry the
    `.githooks/pre-push` main had added: the plan (built from the merged
    Makefile and the index) and the rule disagreed on a tree the merge never
    produces. Staging the overlaid, merged and removed paths leaves the index as
    the merge would: main's files tracked, main's deletions gone, and the
    HQ-only machinery still untracked — exactly the state of a checkout of `main`
    on this host.
    """
    if not files:
        return
    git("add", "-A", "-f", "--", *files, cwd=destination)


def remove_paths(destination: Path, files: list[str]) -> list[str]:
    """Delete `files` from the throwaway tree; return the paths removed."""
    removed: list[str] = []
    for relative in files:
        target = destination / relative
        if target.is_symlink() or target.exists():
            target.unlink()
            removed.append(relative)
    return removed


def unstage_paths(destination: Path, files: list[str]) -> None:
    """Drop `files` from the preview's index, leaving their bytes on disk.

    HQ's checkout after the merge has no index entry for a path `main` deleted:
    the file survives only because it is ignored (or because a seat staged it on
    top of the deletion). The preview has to answer the same way, or a rule that
    reads `git ls-files` sees a tracked file HQ calls untracked.
    `--force-remove` is index-only — unlike `remove_paths` it keeps the bytes.
    """
    if not files:
        return
    git("update-index", "--force-remove", "--", *files, cwd=destination)


def main_side_files(hq: Path, tree: Path, revision: str | None = None) -> list[str]:
    """Files the merge takes from `main`, sorted, noise off.

    A file that exists only on `main` is in neither set the gate used to read:
    HQ *tracks* it, so `ls-files --others --ignored` cannot name it, and the
    branch's HEAD has not got it — ODL-941, where `main` gained
    `docs/craft/finance.md` after the card was cut and `test_craft` failed on a
    card that touched no craft document. A file `main` *repaired* while the
    branch left it alone belongs here too, or the gate runs a document the merge
    will never produce — the other half of ODL-479. A path the branch tracks is
    dropped, so the branch's bytes still win.
    """
    if revision is None:
        revision = integration_commit(hq)
    if revision is None:
        return []
    shared = merge_base(tree, revision)
    return [path for path in carried_paths(
        tree_entries(hq, revision),
        tree_entries(tree, shared) if shared else {},
        tree_entries(tree, "HEAD"),
    ) if not is_noise(path)]


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


def write_blobs(hq: Path, destination: Path, entries: dict[str, tuple[str, str]],
                files: list[str], source: str) -> list[str]:
    """Write `entries`' bytes for `files` into the throwaway tree; return the paths.

    `entries` is `{path: (mode, blob)}` from a tree (`tree_entries`) or from the
    index (`index_entries`). Git's mode travels with the blob, so an executable
    tool stays executable and a symlink stays a symlink.
    """
    copied: list[str] = []
    for relative in files:
        entry = entries.get(relative)
        if entry is None:  # a gitlink (submodule) has no blob to copy
            continue
        mode, object_id = entry
        blob = git_bytes("cat-file", "blob", object_id, cwd=hq)
        if blob.returncode != 0:
            raise SystemExit(f"hq-gate: cannot read {relative} at {source}: "
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


def index_entries(hq: Path, files: list[str]) -> dict[str, tuple[str, str]]:
    """`{path: (mode, blob)}` for `files` as HQ's *index* holds them.

    HQ's index, not its working copy: a staged file's bytes are the ones the
    seat wrote and `git add`ed, and an unstaged edit in the same file is not.
    """
    if not files:
        return {}
    result = git("ls-files", "--stage", "-z", "--", *files, cwd=hq)
    if result.returncode != 0:
        raise SystemExit(f"hq-gate: cannot read HQ's index in {hq}: "
                         f"{result.stderr.strip()[:160]}")
    entries: dict[str, tuple[str, str]] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        # stage 0 is the resolved entry; an unmerged path has 1-3 and no
        # single answer, so it keeps whatever the other sets say (a merge would
        # not take an unmerged index entry either).
        if len(fields) == 3 and fields[2] == "0" and path in files:
            entries[path] = (fields[0], fields[1])
    return entries


def overlay_index(hq: Path, destination: Path, files: list[str]) -> list[str]:
    """Write the bytes of `files` from HQ's index into the throwaway tree.

    The staged set: a path a seat `git add`ed at HQ and has not committed. The
    preview keeps it untracked, exactly as the HQ-only machinery, because no
    commit carries it — the merge will add it when the seat commits.
    """
    return write_blobs(hq, destination, index_entries(hq, files), files, "HQ's index")


def overlay_commit(hq: Path, revision: str, destination: Path, files: list[str]) -> list[str]:
    """Write `revision`'s bytes for `files` into the throwaway tree; return the paths.

    The bytes come from the commit, not from HQ's working copy: an uncommitted
    edit at HQ is not what the merge carries, and the file need not be in HQ's
    checkout at all when that checkout sits on a branch without it. Git's mode
    travels with the blob, so an executable tool stays executable.
    """
    if not files:
        return []
    return write_blobs(hq, destination, tree_entries(hq, revision), files, revision)


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
    ignored = ignored_files(hq)
    staged_at_hq = staged_added_files(hq)
    hq_files = hq_only_files(hq, tracked)
    main_files = main_side_files(hq, tree, revision)
    staged = staged_files(hq, tracked, carried=set(main_files))
    deleted = main_deleted_files(hq, tree, revision)
    # A path main deleted that HQ still carries (ignored on disk, or staged in
    # its index) exists at HQ after the merge: keep HQ's bytes, and remove only
    # what HQ no longer has. Measured 2026-11-21: main deleted the private
    # machinery from the tree while HQ kept it, and every stale card reded
    # `test_craft` on a playbook it never touched.
    kept_ignored, kept_staged = hq_retained_files(deleted, ignored, staged_at_hq)
    kept = set(kept_ignored) | set(kept_staged)
    main_deleted = [path for path in deleted if path not in kept]
    files = sorted(set(hq_files) | set(main_files) | set(staged) | kept)
    modules = modules_from(files)

    # A path both sides changed is not either side's file: a merge applies
    # main's hunks to the branch's text, so the preview has to do the same.
    base_entries: dict[str, tuple[str, str]] = {}
    main_entries: dict[str, tuple[str, str]] = {}
    changed: list[str] = []
    if revision is not None:
        shared = merge_base(tree, revision)
        if shared is not None:
            main_entries = tree_entries(hq, revision)
            base_entries = tree_entries(tree, shared)
            changed = [path for path in both_changed_paths(
                main_entries, base_entries, tree_entries(tree, "HEAD")) if not is_noise(path)]

    if args.list:
        for path in files:
            print(path)
        for path in changed:
            print(f"~{path}")
        for path in main_deleted:
            print(f"-{path}")
        print(f"hq-gate: {len(modules)} HQ-only module(s), {len(files)} file(s) "
              f"({len(staged)} staged at HQ), {len(changed)} both sides changed, "
              f"{len(main_deleted)} deleted by main, {len(kept)} kept from HQ")
        return 0

    if not modules:
        print(f"hq-gate: OK — no HQ-only test module (HQ {hq}, tree {tree})")
        return 0

    print(f"hq-gate: {len(modules)} module(s) to run, {len(files)} file(s) to overlay "
          f"from {hq} ({len(hq_files)} HQ-only, {len(main_files)} from main's side "
          f"of the merge, {len(staged)} staged at HQ); {len(changed)} path(s) both "
          f"sides changed; {len(main_deleted)} path(s) main deleted removed from "
          f"the preview, {len(kept)} kept from HQ")

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
        # The seat's staged batch: bytes from HQ's index, so a rule that reads a
        # playbook still staged no longer reds on the rule alone.
        overlay_index(hq, destination, staged)
        # The merge's own text for a path both sides changed, then the
        # deletions: a path main removed wins over anything an overlay put
        # there, exactly as the merge's `ours`/`theirs` resolution would.
        merged, conflicted = combine_paths(destination, tree, hq, base_entries,
                                           main_entries, changed)
        if changed:
            print(f"hq-gate: {len(merged)} path(s) both sides changed merged as a "
                  f"merge would, {len(conflicted)} kept the branch's bytes")
        # A path main deleted but HQ still carries: HQ's bytes (its working
        # copy, or its index for a staged batch), then out of the index so the
        # preview tracks what HQ's checkout tracks — nothing.
        overlay(hq, destination, kept_ignored)
        overlay_index(hq, destination, kept_staged)
        remove_paths(destination, main_deleted)
        unstage_paths(destination, sorted(kept))
        # The index too, not just the working tree: a rule that asks git what
        # the tree tracks must read the merge's answer.
        stage_overlay(destination, sorted(set(main_files) | set(merged) | set(main_deleted)))
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
          f"{tree} HEAD plus {len(files)} overlaid file(s) ({len(main_files)} from "
          f"main's side of the merge)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
