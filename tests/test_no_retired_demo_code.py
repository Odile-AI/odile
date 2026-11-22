#!/usr/bin/python3
"""The release fence: the meter, the freeze and the HQ demo stay removed.

Three cards removed three mechanisms — t_e237e261 the inference meter,
t_69f1a5f3 the demo's Caddy freeze, t_9d7ee536 the HQ demo host. A removal
holds only if a later card cannot put the mechanism back, so this file is the
repo-wide guard ``make check`` runs: a retired name in live code fails the gate
with the file and the line, not with a sentence in a runbook. It is wired the
way every other gate test is — ``make check`` runs ``unittest discover -s
tests``, so a file under ``tests/`` is in the gate without a Makefile line.

What is scanned
---------------
Every file git sees in the repository — tracked plus untracked and not
ignored — except:

* ``docs/`` and ``deploy/demo-host/evidence/`` — dated history (transcripts,
  the product/security record, live measurements). A card that needs a new
  historical mention puts it there or adds an allowlist entry below,
  deliberately;
* build and runtime trees (``.git``, ``.worktrees``, ``.venv``,
  ``.fresh-clone``, ``__pycache__``, ``node_modules``);
* binary files, and this guard file (it spells the retired names).

The enumeration is ``git ls-files --cached --others --exclude-standard``, not
an ``os.walk``, because the main worktree carries files the repository does
not: the machine's private ``AGENTS.md`` (the operating record), its
private tools and the demo evidence are all in ``.git/info/exclude``. ``make
check`` is run from that worktree by the merge gate, so a walk would fail the
merge on a file no clone has. ``--others`` keeps a planted untracked file in
scope, which is what makes the fence catch a real regression.

The allowlist
-------------
``ALLOW`` names the live files that legitimately carry a retired word: a
negative assertion ("the unit is not found") or a history comment in shipped
bytes whose edit would mean a re-sign. Each entry says why, and three meta-tests
keep the list honest: an entry for a deleted file, for a word the file no
longer carries, or for a file the public build drops fails until the list is
updated.

The positive shape is pinned too: the tenant template routes inference straight
to NEAR AI with no meter placeholder, the committed demo Caddy block carries no
freeze matcher, and the retired ``/judge`` address stays a redirect that no
shipped page prints.
"""

from __future__ import annotations

import ast
import fnmatch
import http.client
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FENCE = Path(__file__).resolve()

#: History, not code. `docs/` carries the dated transcripts and the
#: product/security record; the evidence directory is the demo's own dump of
#: live measurements. A retired word there is a record, not a mechanism.
HISTORY_DIRS = (("docs",), ("deploy", "demo-host", "evidence"))

#: Build/runtime trees. `.git` can be a file in a linked worktree, so the walk
#: skips the name in both shapes.
PRUNE_NAMES = {".git", ".worktrees", ".venv", ".fresh-clone", "__pycache__",
               "node_modules"}

#: (token, pattern). The token is what the allowlist names; the pattern is the
#: surface. Ports are bounded so a build number or a timestamp that happens to
#: end in 8010/8011 is not a hit — and so is a **hex digest** that carries the
#: four digits between hex letters: measured 2026-11-22 (t_e7e5ac73),
#: `release/release.json` named a manifest whose sha256 read `…a7bb8010bc38…`
#: and every card's gate failed on the substring with no port in the file. A
#: port is still caught in every shape it is written in (`:8010`, `--port
#: 8010`, `8010/tcp`), because those neighbours are not hex letters.
RETIRED = (
    ("meter/", re.compile(r"\bmeter/")),
    ("meter.py", re.compile(r"\bmeter\.py")),
    ("METER_API_KEY", re.compile(r"\bMETER_API_KEY\b")),
    ("METER_SHA256", re.compile(r"\bMETER_SHA256\b")),
    ("odile-meter", re.compile(r"\bodile-meter\b")),
    ("8010", re.compile(r"(?<![\d.a-fA-F])8010(?![\da-fA-F])")),
    ("8011", re.compile(r"(?<![\d.a-fA-F])8011(?![\da-fA-F])")),
    ("meter.env", re.compile(r"\bmeter\.env\b")),
    ("meter.key", re.compile(r"\bmeter\.key\b")),
    ("test-meter", re.compile(r"\btest-meter\b")),
    ("@mutation", re.compile(r"@mutation")),
    ("__disabled__", re.compile(r"__disabled__")),
    ("session_guard", re.compile(r"\bsession_guard\b")),
    ("ws_guard", re.compile(r"\bws_guard\b")),
    ("DEMO-UNFREEZE", re.compile(r"\bDEMO-UNFREEZE\b")),
    ("odiledemo", re.compile(r"\bodiledemo\b")),
    ("deploy_demo_host", re.compile(r"\bdeploy_demo_host\b")),
    ("demo-workspace", re.compile(r"\bdemo-workspace\b")),
    ("odile-demo.service", re.compile(r"\bodile-demo\.service\b")),
)

#: The demo apply's own contract: t_ee5aac66's no-prune/no-caps state and the
#: names of the meter shim deleted on 2026-11-15. These names are legitimate
#: elsewhere — the installer and the seed deployer both build units with
#: ``UNIT_TEMPLATE``/``ensure_unit`` — so they are fenced inside
#: ``deploy/demo-host/`` only.
DEMO_HOST = "deploy/demo-host/"
RETIRED_IN_DEMO_HOST = (
    ("prune(", re.compile(r"\bprune\(")),
    # Case-insensitive: the removed code declared them as module constants
    # (`MAX_TURNS = 25`, `MAX_CONCURRENT_SESSIONS = 3`). The verifier's round-1
    # plant of exactly those two lines passed the case-sensitive patterns.
    ("max_concurrent_sessions", re.compile(r"\bmax_concurrent_sessions\b", re.IGNORECASE)),
    ("max_turns", re.compile(r"\bmax_turns\b", re.IGNORECASE)),
    ("odile-demo-public.service", re.compile(r"\bodile-demo-public\.service\b")),
    ("odile-demo-public.timer", re.compile(r"\bodile-demo-public\.timer\b")),
    ("UNIT_TEMPLATE", re.compile(r"\bUNIT_TEMPLATE\b")),
    ("ensure_unit", re.compile(r"\bensure_unit\b")),
    # The boot re-assert's flags, as a CLI option or in prose: the CoS's
    # 2026-11-14 04:05Z relay says to flag `--tune`/`--keep-hash` in the demo
    # apply, and a command line is a surface whether or not `add_argument`
    # created it.
    ("--tune/--keep-hash", re.compile(r"(?<![\w-])--(?:tune|keep-hash)\b")),
    # The deleted meter-adoption shim's own names (2026-11-14 13:55Z: delete
    # it rather than allow it). Fencing the identifiers as well as the meter
    # words means the shim cannot return under a different spelling.
    ("_looks_like_meter", re.compile(r"\b_looks_like_meter\b")),
    ("METER_KEY", re.compile(r"\bMETER_KEY\b")),
    ("METER_UNIT", re.compile(r"\bMETER_UNIT\b")),
    ("METER_UNIT_FILE", re.compile(r"\bMETER_UNIT_FILE\b")),
    ("METER_URL_MARKERS", re.compile(r"\bMETER_URL_MARKERS\b")),
)

#: A retired mechanism can come back as a name, with no body to grep. These
#: files/directories may not exist anywhere outside history.
FORBIDDEN_PATH = re.compile(
    r"(?:^|/)(?:meter\.py|meter\.env|meter\.key|session_guard\.py|ws_guard\.py|"
    r"deploy_demo_host\.py|odile-meter\.service|odile-demo\.service|"
    r"odile-demo-public\.service|odile-demo-public\.timer|DEMO-UNFREEZE\.md|"
    r"test-meter)$")
FORBIDDEN_DIR = re.compile(r"(?:^|/)(?:meter|demo-workspace)(?:/|$)")

#: Live files that name a retired thing on purpose. Keep this list small: a new
#: entry is a decision ("this occurrence must stay"), not a formality.
ALLOW: dict[str, tuple[frozenset[str], str]] = {
    "deploy/demo-host/check_demo_host.sh": (
        frozenset({"odile-meter", "8011", "__disabled__", "ws_guard", "DEMO-UNFREEZE"}),
        "the live check asserts the retired surfaces are absent (unit not-found, "
        "no 8011 listener, zero freeze lines) and names the old guards in its "
        "section comments"),
    "deploy/demo-host/webapp-chrome.js": (
        frozenset({"session_guard"}),
        "shipped bytes say the session menu is the vendor's again because "
        "session_guard.py is gone"),
    "deploy/image/webapp-chrome.js": (
        frozenset({"session_guard"}),
        "the image's copy of the same shipped bytes"),
    "deploy/image/tenant.service": (
        frozenset({"odile-demo.service"}),
        "the unit's lineage comment: the retired demo unit with tenant paths "
        "substituted"),
    "deploy/image/odile-manifest.service": (
        frozenset({"odile-meter"}),
        "ODL-521's comment: the group was renamed from odile-meter, and a "
        "DynamicUser named after the joining group fails"),
    "tests/test_webapp_brand_reassert.py": (
        frozenset({"session_guard"}),
        "the negative assertion's comment names the guard the session menu was "
        "written for"),
    "installer/odile_install.py": (
        frozenset({"deploy_demo_host"}),
        "the signed installer's docstring; one byte means re-sign + release cut "
        "(ODL-517)"),
    "release/odile_install.py": (
        frozenset({"deploy_demo_host"}),
        "the release copy of the same signed docstring"),
}

#: The private publish tool. It builds the public tree from an explicit INCLUDE
#: minus an EXCLUDE, and is itself excluded, so it exists only on HQ and in the
#: main worktree the merge gate runs from.
PUBLIC_TREE_BUILDER = ("tools", "build_public_tree.py")


def public_tree_exclude() -> list[str] | None:
    """The public build's EXCLUDE globs, when the private builder is in reach.

    Round 3's defect: ``ALLOW`` named ``tests/test_sealed_image.py``, which the
    public build drops, so a built public tree failed both allowlist meta-tests
    on a path no reader of the public repository has. The builder is untracked,
    so this returns ``None`` in a card worktree (and in a built public tree):
    the guard skips there and runs where the merge gate runs. The list is parsed
    with ``ast`` rather than imported — the tool is a script, and a shipped test
    should not depend on its module surface.
    """
    try:
        tree = ast.parse(
            ROOT.joinpath(*PUBLIC_TREE_BUILDER).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "EXCLUDE"):
            return list(ast.literal_eval(node.value))
    return None


def is_history(parts: tuple[str, ...]) -> bool:
    return any(parts[:len(prefix)] == prefix for prefix in HISTORY_DIRS)


#: The machine's operating record. It sits at the root of the main worktree,
#: is never tracked, and only matters to a tree without git (an exported
#: tarball that carried it along); with git, ``info/exclude`` already hides it.
PRIVATE_RECORD = "AGENTS.md"


def git_env() -> dict[str, str]:
    """A child git must not inherit the outer repo's location.

    A pre-commit hook exports ``GIT_DIR`` and ``GIT_INDEX_FILE`` pointing at
    the repository being committed. A fixture repo that inherits them runs
    ``git init`` against the *real* gitdir and then lists the wrong index;
    ``git ls-files`` would answer for the caller's repository instead of the
    tree in hand. ``tests/test_manifest.py`` scrubs the same variables for
    the same reason.
    """
    return {key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")}


def repository_files(root: Path = ROOT) -> list[str]:
    """The files git sees: tracked plus untracked and not ignored.

    ``git ls-files`` is the point, not a convenience. The merge gate runs
    ``make check`` in the main worktree, which carries the machine's private
    ``AGENTS.md`` (the machine's private operating record) and its
    private tools; ``.git/info/exclude`` hides them from the repository, so
    they are not code and the fence must not scan them. ``--others`` keeps a
    planted untracked file in scope, which is how the fence catches a
    regression before it is committed.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, check=True, env=git_env()).stdout
    except (OSError, subprocess.CalledProcessError):
        # A tree without git (an exported tarball): walk it, and still leave
        # the machine's private operating record out when it rides along.
        names: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            parts = here.relative_to(root).parts
            if parts and (parts[0] in PRUNE_NAMES or is_history(parts)):
                dirnames[:] = []
                continue
            dirnames[:] = sorted(name for name in dirnames if name not in PRUNE_NAMES)
            for name in sorted(filenames):
                if name in PRUNE_NAMES:
                    continue
                if not parts and name == PRIVATE_RECORD:
                    continue
                names.append((here / name).relative_to(root).as_posix())
        return names
    return [name for name in out.decode("utf-8", "surrogateescape").split("\0") if name]


def live_files(root: Path = ROOT) -> list[tuple[str, Path]]:
    """Every non-history, non-binary repository file, repo-relative."""
    files: list[tuple[str, Path]] = []
    for rel in repository_files(root):
        parts = tuple(Path(rel).parts)
        if any(part in PRUNE_NAMES for part in parts) or is_history(parts):
            continue
        path = root / rel
        if root == ROOT and path == FENCE:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data:  # an image, a raw disk, a tarball
            continue
        files.append((rel, path))
    return sorted(files)


def patterns_for(rel: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    if rel.startswith(DEMO_HOST):
        return RETIRED + RETIRED_IN_DEMO_HOST
    return RETIRED


def scan_text(rel: str, text: str) -> list[str]:
    """Every unallowlisted retired surface in one file's text, as ``path:line``."""
    found: list[str] = []
    allowed = ALLOW.get(rel, (frozenset(), ""))[0]
    for token, pattern in patterns_for(rel):
        if token in allowed:
            continue
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            line = text.splitlines()[line_no - 1].strip()
            found.append(f"{rel}:{line_no}: retired {token!r} — {line[:160]}")
    return found


def occurrences() -> list[str]:
    """Every unallowlisted retired surface, as ``path:line`` with its text."""
    found: list[str] = []
    for rel, path in live_files():
        if FORBIDDEN_PATH.search(rel) or FORBIDDEN_DIR.search(rel):
            found.append(f"{rel}: a retired path")
        text = path.read_text(encoding="utf-8", errors="replace")
        found.extend(scan_text(rel, text))
    return found


class RetiredCodeTests(unittest.TestCase):
    """The repo-wide fence. One failure lists every violation with file:line."""

    def test_no_live_file_names_a_retired_mechanism(self) -> None:
        found = occurrences()
        if found:
            self.fail("retired code is back in the tree:\n  " + "\n  ".join(found))


class PatternTests(unittest.TestCase):
    """The round-1 regressions: the removed constants, the timer and the flags.

    The fence file itself is skipped by ``live_files`` (it must spell the
    names), so these plants exercise ``scan_text`` on text, not on the tree.
    """

    def tokens(self, rel: str, text: str) -> list[str]:
        return [found.split("retired ", 1)[1].split("'")[1]
                for found in scan_text(rel, text)]

    def test_the_removed_caps_constants_are_caught(self) -> None:
        # The exact two lines the verifier planted (`Ran 8 tests ... OK` on
        # round 1) and the CoS's 13:55Z "still flag MAX_TURNS,
        # MAX_CONCURRENT_SESSIONS".
        self.assertEqual(
            sorted(self.tokens("deploy/demo-host/planted.py",
                               "MAX_TURNS = 25\nMAX_CONCURRENT_SESSIONS = 3\n")),
            ["max_concurrent_sessions", "max_turns"])

    def test_the_demo_timer_is_caught(self) -> None:
        self.assertEqual(
            self.tokens("deploy/demo-host/planted.py",
                        "install -m 0644 x /etc/systemd/system/"
                        + "odile-demo-public" + ".timer\n"),
            ["odile-demo-public.timer"])
        self.assertRegex("deploy/units/" + "odile-demo-public" + ".timer",
                         FORBIDDEN_PATH)

    def test_the_boot_flags_are_caught_without_add_argument(self) -> None:
        self.assertEqual(
            sorted(set(self.tokens("deploy/demo-host/planted.py",
                                   "apply --tune --keep-hash\n"))),
            ["--tune/--keep-hash"])

    def test_the_deleted_shim_names_are_caught(self) -> None:
        for name in ("_looks_like_meter", "METER_KEY", "METER_UNIT_FILE",
                     "METER_URL_MARKERS", "METER_UNIT"):
            with self.subTest(name=name):
                self.assertEqual(self.tokens("deploy/demo-host/planted.py",
                                             f"{name} = None\n"), [name])

    def test_a_clean_demo_host_text_has_no_hits(self) -> None:
        self.assertEqual(self.tokens("deploy/demo-host/planted.py",
                                     "provider: Near AI\nbase_url: https://cloud-api.near.ai/v1\n"),
                         [])

    def test_a_hex_digest_is_not_a_port(self) -> None:
        # Measured 2026-11-22 (t_e7e5ac73): this cut's release.json named a
        # manifest whose sha256 carried `…a7bb8010bc38…`, and the gate failed
        # on the substring while the file named no port at all.
        self.assertEqual(
            self.tokens("release/release.json",
                        '"sha256": "7740b2456cad87d512cfcc0c1aa82549b6acdac815a7bb8010bc38'
                        'aeb666009e"'),
            [])
        # A number that merely ends in the digits is not a port either — the
        # bound the pattern already promised.
        self.assertEqual(self.tokens("deploy/demo-host/planted.py", "build 2026018010"), [])

    def test_the_retired_ports_are_still_caught(self) -> None:
        # The bound must not make a real 8010/8011 invisible: every shape a
        # port is written in has a non-hex neighbour.
        for text, token in (
            ("127.0.0.1:8010", "8010"),
            ("--port 8010", "8010"),
            ("8010/tcp", "8010"),
            ('listen ":8011"', "8011"),
            ("localhost:8011 {", "8011"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.tokens("deploy/demo-host/planted.py", text), [token])


class AllowlistTests(unittest.TestCase):
    """The allowlist stays small and current: no dead entries, no absent files."""

    def test_every_allowlisted_file_exists(self) -> None:
        for rel in ALLOW:
            with self.subTest(path=rel):
                self.assertTrue((ROOT / rel).is_file(),
                                f"{rel} is allowlisted but not in the tree; "
                                "remove the entry")

    def test_no_allowlist_entry_names_a_file_the_public_build_drops(self) -> None:
        """An allowlist entry must survive the public build.

        Round 3 shipped an entry for `tests/test_sealed_image.py`, which
        `tools/build_public_tree.py` excludes, so the built public tree failed
        this class on a path it never carries. The builder is private; where it
        is absent (a card worktree, the public tree) the check skips.
        """
        exclude = public_tree_exclude()
        if exclude is None:
            self.skipTest("the private public-tree builder is not in this tree")
        dropped = sorted(rel for rel in ALLOW
                         if any(fnmatch.fnmatch(rel, glob) for glob in exclude))
        self.assertEqual(dropped, [],
                         "an ALLOW entry names a file the public build drops; "
                         "reword the file or move the entry to history")

    def test_every_allowlisted_word_still_occurs(self) -> None:
        for rel, (tokens, _reason) in ALLOW.items():
            text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
            for token, pattern in patterns_for(rel):
                if token not in tokens:
                    continue
                with self.subTest(path=rel, token=token):
                    self.assertRegex(
                        text, pattern,
                        f"{rel} no longer carries {token!r}; remove it from ALLOW")


class ScopeTests(unittest.TestCase):
    """git decides what is code: a private file is out, an untracked plant in.

    Round 2's defect: the walk scanned the *main worktree's* private
    ``AGENTS.md`` (hidden by ``.git/info/exclude``), so the merge gate's
    ``make check`` failed on a file no clone carries. The other half matters
    too — an untracked, unignored plant must still be caught before anyone
    commits it — so both halves are pinned here.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="odile-fence-scope-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root,
                       capture_output=True, check=True, env=git_env())
        (self.root / ".git" / "info" / "exclude").write_text(
            "/AGENTS.md\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text(
            "| Inference | reached through odile" + "-meter on the guest |\n",
            encoding="utf-8")
        (self.root / "live.py").write_text("provider: Near AI\n", encoding="utf-8")

    def names(self) -> list[str]:
        return [rel for rel, _path in live_files(self.root)]

    def test_a_file_git_excludes_is_not_scanned(self) -> None:
        self.assertNotIn("AGENTS.md", self.names())
        hits = [hit for rel, path in live_files(self.root)
                for hit in scan_text(rel, path.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [])

    def test_an_untracked_unignored_file_is_scanned(self) -> None:
        (self.root / "plant.py").write_text("odile" + "-meter\n", encoding="utf-8")
        hits = [hit for rel, path in live_files(self.root)
                for hit in scan_text(rel, path.read_text(encoding="utf-8"))]
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].startswith("plant.py:1: retired 'odile-meter'"), hits)

    def test_a_tree_without_git_still_skips_the_private_record(self) -> None:
        shutil.rmtree(self.root / ".git")
        self.assertEqual(self.names(), ["live.py"])


class TenantShapeTests(unittest.TestCase):
    """The shipped tenant profile points at NEAR AI, with no meter surface."""

    def test_the_template_routes_inference_straight_to_near_ai(self) -> None:
        text = (ROOT / "templates" / "config.yaml").read_text(encoding="utf-8")
        for line in ("name: Near AI",
                     "base_url: https://cloud-api.near.ai/v1",
                     "key_env: HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY"):
            with self.subTest(line=line):
                self.assertIn(line, text)
        # The installer's own self-test spells this as replace("parameter", "")
        # so a future word like "parameter" cannot satisfy the scan by accident.
        self.assertNotIn("meter", text.replace("parameter", "").lower(),
                         "the tenant template names the retired meter")

    def test_the_demo_caddy_source_carries_no_freeze(self) -> None:
        text = (ROOT / "deploy" / "caddy" / "demo-tenant-site.caddy").read_text(
            encoding="utf-8")
        for token, pattern in RETIRED:
            if token not in ("@mutation", "__disabled__"):
                continue
            self.assertNotRegex(text, pattern, f"the committed block names {token!r}")
        self.assertNotIn("@update", text, "the committed block carries a self-update matcher")
        self.assertEqual(text.count("# BEGIN odile-demo-public"), 1)
        self.assertEqual(text.count("# END odile-demo-public"), 1)


class DemoEgressTests(unittest.TestCase):
    """The egress sentence is retired, and the machine still proves the position.

    The HQ filter was a loopback filter — its own header said "Internet traffic
    is untouched" — so ODL-501's sentence ("the demo profile cannot reach the
    internet except the meter and DNS") was false and is retired (t_d969339e).
    The surviving record is the runbook paragraph plus the check's egress block;
    this pins the pair so a later card cannot quietly drop one of them and leave
    the other claiming the network is closed.
    """

    def test_the_check_measures_the_egress_position(self) -> None:
        text = (ROOT / "deploy" / "demo-host" / "check_demo_host.sh").read_text(
            encoding="utf-8")
        # The two filter shapes the block proves, and the internet leg. A later
        # change that drops one of them leaves the runbook claiming more than
        # the check measures (verifier, round 1, t_d969339e).
        for line in ("command -v nft",
                     "for IPT in iptables iptables-legacy",
                     '"$IPT" -S',
                     "no loaded nft or iptables ruleset",
                     "the demo shell reaches the public internet"):
            with self.subTest(line=line):
                self.assertIn(line, text)

    def test_the_runbook_records_it(self) -> None:
        text = (ROOT / "docs" / "runbooks" / "demo-public.md").read_text(
            encoding="utf-8")
        for line in ("### Egress: no `nft` or `iptables` ruleset",
                     "Internet traffic is untouched",
                     "cannot reach any other loopback listener",
                     "fails if a loaded `nft` or `iptables` ruleset appears here"):
            with self.subTest(line=line):
                self.assertIn(line, text)
        # The paragraph must not widen the claim past what the block proves.
        self.assertNotIn("fails if a packet filter appears", text)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_app(db: Path) -> tuple[subprocess.Popen, int]:
    """The real waitlist server on a free port, against a throwaway database."""
    port = free_port()
    env = dict(os.environ, ODILE_BIND="127.0.0.1", ODILE_PORT=str(port),
               ODILE_RATE_LIMIT="1000", ODILE_DB=str(db))
    proc = subprocess.Popen([sys.executable, str(ROOT / "app.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1).status == 200:
                return proc, port
        except OSError:
            time.sleep(0.05)
    out = proc.communicate(timeout=5)[0].decode("utf-8", "replace")
    raise RuntimeError("the server did not start:\n" + out)


def stop_app(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


class JudgeSurfaceTests(unittest.TestCase):
    """`/judge` is retired as a page, not as an address (ODL-901)."""

    def test_no_shipped_page_prints_the_retired_judge_address(self) -> None:
        surfaces = [rel for rel, _path in live_files()
                    if rel == "README.md" or rel.endswith(".html")]
        self.assertIn("README.md", surfaces)
        self.assertTrue(any(rel.startswith("web/") for rel in surfaces),
                        "the shipped pages under web/ were not scanned")
        for rel in surfaces:
            with self.subTest(surface=rel):
                self.assertNotIn("odile.fyi/judge",
                                 (ROOT / rel).read_text(encoding="utf-8"),
                                 f"{rel} prints the retired /judge address")


class JudgeRouteTests(unittest.TestCase):
    """The address a posted card prints still answers, permanently."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="odile-fence-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.proc, cls.port = start_app(Path(cls.tmp.name) / "waitlist.db")
        cls.addClassCleanup(stop_app, cls.proc)

    def request(self, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_judge_answers_permanently_at_the_tenant_login(self) -> None:
        status, headers, body = self.request("/judge")
        self.assertEqual(status, 301)
        self.assertEqual(headers.get("Location"), "https://demo.odile.fyi/login")
        self.assertEqual(body, b"")

    def test_judge_carries_no_query_string(self) -> None:
        status, headers, _ = self.request("/judge?next=/elsewhere")
        self.assertEqual(status, 301)
        self.assertEqual(headers.get("Location"), "https://demo.odile.fyi/login")


if __name__ == "__main__":
    unittest.main(verbosity=2)
