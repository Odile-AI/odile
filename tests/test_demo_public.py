"""The demo's /verify lede — ODL-503 finding 1.

The page a judge is sent to is the tenant's own access report. On Odile's demo
machine the customer-tenant row is false in both clauses: Odile holds the boot
key and the sign-in is the shared `judge` account. The demo layer therefore
replaces the one lede paragraph on the demo machine's copy of the release
template; the customer's installer page is untouched.

These tests pin three things:

* the lede is assembled from sentences a reviewer can find verbatim in
  `docs/CLAIMS.md` or the published record of the lede
  (`docs/runbooks/demo-public.md`, the ODL-503 decision) — a coined demo
  sentence is a finding. `web/judge.html`, the second source until ODL-901
  retired the page, is gone;
* the injection is anchored on the tenant block, changes nothing outside it and
  is idempotent — and every page the signed installer/release ships still
  carries the general row, so a customer machine renders it;
* the tenant-side program writes the page and its rollback copy, and the
  deploy/check programs read the same committed lede.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "deploy" / "demo-host"
LEDE = DEMO / "verify-lede.html"
#: The demo lede's second approved source. `web/judge.html` carried the copy
#: until ODL-901 retired it; the runbook publishes the same paragraph as the
#: ODL-503 decision, so a reviewer can still check every sentence after the page
#: is gone.
LEDE_RECORD = ROOT / "docs" / "runbooks" / "demo-public.md"
CUSTOMER_SENTENCE = "No one at Odile can log in to a tenant machine."
INSTALLERS = (ROOT / "installer" / "odile_install.py",
              ROOT / "release" / "odile_install.py")


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def embedded_page(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"VERIFY_TEMPLATE = r'''(.*?)'''", text, re.S)
    assert match is not None, f"{path} carries no VERIFY_TEMPLATE"
    return match.group(1)


def render_page(page: str, measurement: str = "9f" * 48) -> str:
    """Render *page* through the shipped /verify program (t_4fcd465f).

    The demo's template is read by `odile-verify.service` now, not by the
    first-visit renderer; this builds the Machine the renderer needs without
    touching a machine root, so the test checks the bytes a browser gets.
    """
    verify = load(ROOT / "verify" / "verify.py", "demo_public_verify_render")
    machine = verify.Machine(root=Path("/"), now=verify.parse_iso("2026-11-14T00:00:00Z"))
    machine.values = {
        "stamp": "2026-11-14T00:00:00Z", "measurement": measurement,
        "measurement_short": measurement[:16] if measurement else "",
        "release": "2026.11.2" if measurement else "", "vcpus": "4" if measurement else "",
        "report_fingerprint": "", "fetched_at": "2026-11-14T00:00:00Z" if measurement else "",
        "boot_key": "", "host_key": "", "signing_key": "",
        "state": "available" if measurement else "unavailable",
        "state_text": "" if measurement else "The CPU has not produced a report yet",
    }
    return verify.render(machine, page)


def flatten(text: str) -> str:
    """One line, no markup, no backticks: the shape a sentence check needs."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("`", "").replace("**", "")
    return re.sub(r"\s+", " ", text).strip().casefold()


def sentences(markup: str) -> list[str]:
    """The lede's sentences, with the paragraph tags stripped."""
    body = re.sub(r"^<p[^>]*>|</p>$", "", markup.strip())
    return [part.strip() for part in re.split(r"(?<=\.)\s+", body) if part.strip()]


def published_lede(path: Path) -> str:
    """The lede as the runbook publishes it: one `>` block, unwrapped.

    The block is the record of the copy a reviewer approved (ODL-503), so the
    ledger below can still be checked sentence by sentence now that the page
    which carried it is gone. A missing block is a failure, not an empty pass:
    an empty string would make every sentence check vacuously true.
    """
    block: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(">"):
            block.append(line[1:].strip())
        elif block:
            break
    if not block:
        raise AssertionError(f"{path} publishes no quoted lede")
    return flatten(" ".join(block))


class LedeSourceTests(unittest.TestCase):
    """The copy is approved copy, not a sentence invented for the demo."""

    @classmethod
    def setUpClass(cls):
        cls.markup = LEDE.read_text(encoding="utf-8").strip()
        cls.claims = flatten((ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8"))
        cls.record = published_lede(LEDE_RECORD)

    def test_the_lede_is_one_paragraph_and_no_slot_or_markup_smuggling(self):
        text = self.markup
        self.assertEqual(text.rstrip("\n").count("\n"), 0, "the lede is one line")
        self.assertTrue(text.startswith('<p class="lede">'))
        self.assertTrue(text.endswith("</p>"))
        self.assertEqual(text.count("<p"), 1)
        self.assertNotIn("{{", text)
        self.assertNotIn("<script", text)

    def test_every_sentence_is_in_the_deck_or_the_published_record_verbatim(self):
        for sentence in sentences(self.markup):
            with self.subTest(sentence=sentence):
                self.assertTrue(flatten(sentence) in self.claims or flatten(sentence) in self.record,
                                f"not a published sentence: {sentence!r}")

    def test_the_lede_names_the_boot_key_and_the_shared_sign_in(self):
        text = flatten(self.markup)
        self.assertIn("odile holds this machine's boot key and can log in", text)
        self.assertIn("the demo password is public by design", text)
        # ODL-563: the deck row (docs/CLAIMS.md §The demo machine), verbatim.
        # "Every judge who signs in lands in the same workspace." was coined for
        # the demo and was in no row; it must not come back.
        self.assertIn("every judge sees these sessions and files", text)
        self.assertNotIn("lands in the same workspace", text)
        self.assertNotIn(flatten(CUSTOMER_SENTENCE), text)


class InjectionTests(unittest.TestCase):
    """The tenant-side rule: one paragraph, inside the tenant block, idempotent."""

    @classmethod
    def setUpClass(cls):
        cls.apply = load(DEMO / "apply_demo_public.py", "demo_apply_public_test")
        cls.lede = LEDE.read_text(encoding="utf-8").strip()

    def test_every_shipped_page_still_carries_the_customer_row(self):
        for path in INSTALLERS:
            with self.subTest(installer=path.name):
                page = embedded_page(path)
                block = self.apply.tenant_block(page)
                self.assertIn(CUSTOMER_SENTENCE, block)

    def test_the_injection_replaces_the_tenant_lede_and_nothing_else(self):
        for path in INSTALLERS:
            with self.subTest(installer=path.name):
                page = embedded_page(path)
                injected = self.apply.inject_lede(page, self.lede)
                before, after = self.apply.tenant_block(page), self.apply.tenant_block(injected)
                self.assertIn(self.lede, after)
                self.assertNotIn(CUSTOMER_SENTENCE, after)
                # The rest of the page — the demo block, the report card, the
                # CSS — is byte-identical.
                head = page.index(before)
                new_head = injected.index(after)
                self.assertEqual(page[:head] + page[head + len(before):],
                                 injected[:new_head] + injected[new_head + len(after):])
                self.assertEqual(injected.count(self.lede), 1)

    def test_the_injection_is_idempotent(self):
        page = embedded_page(INSTALLERS[0])
        once = self.apply.inject_lede(page, self.lede)
        twice = self.apply.inject_lede(once, self.lede)
        self.assertEqual(once, twice)

    def test_a_page_without_the_tenant_block_fails_loudly(self):
        with self.assertRaises(ValueError):
            self.apply.inject_lede("<html><body><h1>Verify</h1></body></html>", self.lede)

    def test_two_tenant_blocks_fail_loudly(self):
        page = ("<!--tenant-->\n<p class=\"lede\">one</p>\n<!--/tenant-->\n"
                "<!--tenant-->\n<p class=\"lede\">two</p>\n<!--/tenant-->\n")
        with self.assertRaises(ValueError):
            self.apply.inject_lede(page, self.lede)

    def test_a_tenant_block_with_two_ledes_fails_loudly(self):
        page = ("<!--tenant-->\n<p class=\"lede\">one</p>\n<p class=\"lede\">two</p>\n"
                "<!--/tenant-->\n")
        with self.assertRaises(ValueError):
            self.apply.inject_lede(page, self.lede)

    def test_a_lede_that_is_not_one_paragraph_is_refused(self):
        page = embedded_page(INSTALLERS[0])
        for bad in ("<p>plain</p>", '<p class="lede">a</p><p class="lede">b</p>',
                    '<p class="lede">{{MEASUREMENT}}</p>', '<p class="lede">a\nb</p>'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.apply.inject_lede(page, bad)


class RenderTests(unittest.TestCase):
    """What a browser actually reads, through the release renderer itself."""

    @classmethod
    def setUpClass(cls):
        cls.apply = load(DEMO / "apply_demo_public.py", "demo_apply_render_test")
        cls.lede = LEDE.read_text(encoding="utf-8").strip()
        cls.page = embedded_page(INSTALLERS[0])
        cls.measurement = "9f" * 48

    def render(self, page: str) -> str:
        return render_page(page, self.measurement)

    def test_a_customer_page_renders_the_general_row(self):
        rendered = self.render(self.page)
        self.assertIn(CUSTOMER_SENTENCE, rendered)
        self.assertNotIn(self.lede, rendered)

    def test_the_demo_page_renders_the_demo_row_and_keeps_the_live_measurement(self):
        rendered = self.render(self.apply.inject_lede(self.page, self.lede))
        first = re.search(r'<p class="lede">.*?</p>', rendered, re.S)
        self.assertIsNotNone(first)
        self.assertEqual(first.group(0), self.lede)
        self.assertNotIn(CUSTOMER_SENTENCE, rendered)
        self.assertIn(self.measurement[:16], rendered)


class TenantProgramTests(unittest.TestCase):
    """ensure_verify_lede writes the page and keeps the release copy for rollback."""

    def setUp(self):
        self.apply = load(DEMO / "apply_demo_public.py", "demo_apply_write_test")
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-demo-verify-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.page_path = self.tmp / "verify.html"
        self.lede_path = self.tmp / "verify-lede.html"
        self.backup_path = self.tmp / "verify.html.release"
        self.page_path.write_text(embedded_page(INSTALLERS[0]), encoding="utf-8")
        shutil.copyfile(LEDE, self.lede_path)
        self.apply.VERIFY_TEMPLATE = self.page_path
        self.apply.VERIFY_LEDE = self.lede_path
        self.apply.VERIFY_BACKUP = self.backup_path
        # lede_source() looks beside the program first, as the dry run needs;
        # point that at the scratch directory so the test owns every path.
        self.apply.__file__ = str(self.tmp / "apply_demo_public.py")

    def test_the_first_run_writes_the_demo_page_and_keeps_the_release_bytes(self):
        release = self.page_path.read_bytes()
        self.assertTrue(self.apply.ensure_verify_lede(dry=False))
        written = self.page_path.read_text(encoding="utf-8")
        self.assertEqual(self.apply.tenant_block(written).strip(),
                         LEDE.read_text(encoding="utf-8").strip())
        self.assertEqual(self.backup_path.read_bytes(), release)

    def test_a_second_run_is_same_and_does_not_rewrite(self):
        self.apply.ensure_verify_lede(dry=False)
        digest = hashlib.sha256(self.page_path.read_bytes()).hexdigest()
        self.assertFalse(self.apply.ensure_verify_lede(dry=False))
        self.assertEqual(hashlib.sha256(self.page_path.read_bytes()).hexdigest(), digest)

    def test_a_dry_run_plans_and_writes_nothing(self):
        release = self.page_path.read_bytes()
        self.assertTrue(self.apply.ensure_verify_lede(dry=True))
        self.assertEqual(self.page_path.read_bytes(), release)
        self.assertFalse(self.backup_path.exists())

    def test_a_missing_lede_file_stops_loudly(self):
        self.lede_path.unlink()
        with self.assertRaises(SystemExit):
            self.apply.ensure_verify_lede(dry=False)


class DeployAndCheckTests(unittest.TestCase):
    """One committed lede: the tenant program, the deploy's uploads and the check."""

    def test_the_deploy_uploads_the_lede_under_the_name_the_apply_reads(self):
        deploy = load(DEMO / "deploy_demo_public.py", "demo_deploy_public_test")
        apply = load(DEMO / "apply_demo_public.py", "demo_apply_upload_test")
        names = [name for name, _ in deploy.UPLOADS]
        self.assertIn(apply.VERIFY_LEDE.name, names)
        source = DEMO / apply.VERIFY_LEDE.name
        self.assertTrue(source.is_file())
        self.assertEqual(source.read_text(encoding="utf-8"), LEDE.read_text(encoding="utf-8"))

    def test_the_check_reads_the_same_lede_and_knows_the_customer_row(self):
        check = load(DEMO / "check_demo_public.py", "demo_check_public_test")
        self.assertEqual(check.VERIFY_LEDE.read_text(encoding="utf-8").strip(),
                         LEDE.read_text(encoding="utf-8").strip())
        body = "<main><h1>Verify</h1>%s<p class=\"lede\">The machine is an AMD…</p></main>" % \
               LEDE.read_text(encoding="utf-8")
        self.assertEqual(check.lede_problem(body), "")
        self.assertIn(CUSTOMER_SENTENCE, check.lede_problem(body.replace(
            LEDE.read_text(encoding="utf-8").strip(),
            '<p class="lede">%s The only sign-in is yours.</p>' % CUSTOMER_SENTENCE)))

    def test_the_deploy_refuses_a_page_without_exactly_one_demo_check_section(self):
        """The deploy's own verify gate (ODL-563): the marker, not just the line.

        A page with no section passes "some text" checks and serves a stale
        demo; a page with two sections is ambiguous about which line a judge
        copies. The deploy records a FAIL for either.
        """
        deploy = load(DEMO / "deploy_demo_public.py", "demo_deploy_marker_test")
        section = (DEMO / "verify-command.html").read_text(encoding="utf-8").strip()
        page = '<main><p class="lede">%s</p></main>' % LEDE.read_text(encoding="utf-8").strip()
        self.assertIn("demo-check", deploy.demo_check_problem(page))
        self.assertEqual(deploy.demo_check_problem(page + section), "")
        self.assertIn("2", deploy.demo_check_problem(page + section + section))

    def test_the_freshness_gate_pins_the_five_minute_row(self):
        """A stale report fails the gate, not just a missing one (t_145ca49c).

        The regression was a green gate over a three-day-old report: the
        measurement matched, so every existing row passed. The age itself has
        to be a row, bounded by the claims sentence, not by a timer's say-so.
        """
        from datetime import datetime, timezone

        check = load(DEMO / "check_demo_public.py", "demo_check_freshness_test")
        self.assertEqual(check.REPORT_MAX_AGE_SECONDS, 600)
        base = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()
        report = {"fetched_at": "2026-11-17T11:55:00Z"}
        self.assertEqual(check.freshness_problem(report, now=base), "")
        self.assertEqual(check.freshness_problem(report, now=base + 300), "")
        self.assertIn("11.0 minutes", check.freshness_problem(report, now=base + 360))
        self.assertIn("no fetched_at", check.freshness_problem({}, now=base))
        self.assertIn("ISO-8601", check.freshness_problem(
            {"fetched_at": "yesterday"}, now=base))
        self.assertIn("future", check.freshness_problem(
            {"fetched_at": "2026-11-17T12:05:00Z"}, now=base))

    def test_the_deploy_ships_the_refresh_units_the_apply_installs(self):
        """The demo's timer is committed, uploaded and installed in one deploy.

        A machine rebuilt from the image has the boot report only (ODL-397);
        the five-minute row is true only if the deploy puts the timer back.
        """
        deploy = load(DEMO / "deploy_demo_public.py", "demo_deploy_refresh_test")
        apply = load(DEMO / "apply_demo_public.py", "demo_apply_refresh_test")
        names = [name for name, _ in deploy.UPLOADS]
        for name in ("attest-refresh.sh", "odile-attest-refresh.service",
                     "odile-attest-refresh.timer"):
            self.assertIn(name, names)
        self.assertTrue((ROOT / "deploy" / "attest-refresh" / "refresh.sh").is_file())
        self.assertTrue((DEMO / "odile-attest-refresh.service").is_file())
        self.assertTrue((DEMO / "odile-attest-refresh.timer").is_file())
        self.assertEqual(apply.ATTEST_TIMER_UNIT, "odile-attest-refresh.timer")
        self.assertEqual(apply.ATTEST_REFRESH_TARGET.name, "attest-refresh.sh")
        self.assertEqual(apply.ATTEST_FILE.name, "attestation.json")

    def test_the_deploy_refuses_a_tree_whose_apply_predates_the_no_rewrite_fix(self):
        """The fence covers the tree that deploys, not only `main` (t_262f7168).

        On 2026-11-17 the live check lost the seed's messages and gained one
        session, while `main`'s own fence stayed green — the deploy had run from
        a branch tree, and this program ships the `apply_demo_public.py` that
        sits beside it. So the deploy reads the marker out of the file it is
        about to upload and exits 2 before DNS or any upload.
        """
        deploy = load(DEMO / "deploy_demo_public.py", "demo_deploy_apply_gate_test")
        apply_text = (DEMO / "apply_demo_public.py").read_text(encoding="utf-8")
        self.assertEqual(deploy.apply_problem(DEMO / "apply_demo_public.py"), "")
        # The pair cannot drift: the line the deploy looks for is the line the
        # program says it keeps.
        contract = [line for line in apply_text.splitlines()
                    if line.startswith("APPLY_CONTRACT")]
        self.assertEqual(len(contract), 1)
        self.assertIn(deploy.APPLY_MARKER, contract[0])

        with tempfile.TemporaryDirectory(prefix="odile-old-apply-") as tmp:
            planted = Path(tmp) / "apply_demo_public.py"
            planted.write_text(apply_text.replace(deploy.APPLY_MARKER, ""), encoding="utf-8")
            self.assertIn("no-rewrite", deploy.apply_problem(planted))

            # The whole program, not only the helper: no DNS and no console
            # before the refusal.
            deploy.APPLY_SOURCE = planted
            with mock.patch.object(deploy.create_dns, "ensure",
                                   side_effect=AssertionError("DNS ran before the gate")), \
                 mock.patch.object(deploy.tenant_console, "connect_any",
                                   side_effect=AssertionError("a console opened")), \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(deploy.main([]), 2)
            self.assertIn("no-rewrite", stderr.getvalue())
            # A tree with no apply at all is refused for the same reason: the
            # deploy would otherwise upload nothing and run the old program.
            deploy.APPLY_SOURCE = Path(tmp) / "absent.py"
            self.assertIn("beside the deploy program", deploy.apply_problem())


MATCH = "9f" * 48


class _StubTransport:
    """The deploy's `http`, answered from one stubbed `/verify` body.

    Every other row `verify_public` records is made to pass, so the only
    possible FAIL is the marker row under test. That is the shape that makes
    deleting the `record(section_problem == "", …)` call redden the suite.
    """

    def __init__(self, module, verify_body: str) -> None:
        self.module = module
        self.verify_body = verify_body

    def _login_page(self) -> bytes:
        head = "".join(f'<link rel="icon" href="./{name}">'
                       for name in ("favicon.ico", "icon-32.png",
                                    "apple-touch-icon.png"))
        return ("<!doctype html><html><head><title>Sign in — Odile</title>"
                + head + self.module.CREDIT_COMMENT + "</head><body>"
                '<form data-provider="basic" aria-label="Sign in">'
                "This is an Odile demo instance.</form></body></html>").encode()

    def __call__(self, url: str, *, method: str = "GET", body: bytes | None = None,
                 headers: dict[str, str] | None = None,
                 cookie: str = "") -> tuple[int, bytes, dict]:
        parts = urllib.parse.urlsplit(url)
        route = parts.path
        if parts.netloc == self.module.MACHINE_HOST:
            return 200, b"the machine's own name answers", {}
        if route == "/login":
            return 200, self._login_page(), {
                "Strict-Transport-Security": "max-age=31536000",
                "X-Robots-Tag": "noindex"}
        for name, _pin, content_type in self.module.ICONS:
            if route == "/" + name:
                if method == "HEAD":
                    return 200, b"", {"Content-Length": "1"}
                return 200, (ROOT / "web" / name).read_bytes(), {
                    "Content-Type": content_type,
                    "Cache-Control": "public, max-age=31536000, immutable"}
        if route in ("/favicon.ico/../../etc/passwd", "/..%2f..%2fetc%2fpasswd"):
            return 404, b"not found", {}
        if route == "/":
            if cookie:
                return 200, b'<div id="root"></div>', {}
            return 302, b"", {"Location": "https://demo.odile.fyi/login"}
        if route == "/api/sessions":
            if not cookie:
                return 401, b"", {}
            return 200, json.dumps(
                {"sessions": [{"title": "Start here — Odile demo"}]}).encode(), {}
        if route == "/verify":
            return 200, self.verify_body.encode("utf-8"), {}
        if route == "/attestation":
            return 200, json.dumps({"measurement": MATCH}).encode(), {}
        raise AssertionError(f"the stub got an unexpected URL: {url}")


def served_page(pairs: int) -> str:
    """A /verify body with *pairs* demo-check sections and the live measurement.

    The command line is present even at zero pairs, so a no-section page still
    passes the command row and the marker row is the only FAIL.
    """
    lede = (DEMO / "verify-lede.html").read_text(encoding="utf-8").strip()
    command = ((DEMO / "verify-command.html").read_text(encoding="utf-8")
               .strip().replace("{{MEASUREMENT}}", MATCH))
    line = f'<p class="caption">./odile-verify https://demo.odile.fyi --expect {MATCH}</p>'
    return (f'<main>{lede}<p>{MATCH}</p>{line}'
            + (command + "\n") * pairs + "</main>")


class VerifyPublicSectionTests(unittest.TestCase):
    """ODL-563 N3: the deploy's marker gate is driven, not just its helper.

    `demo_check_problem`'s own tests stay green if the `record(section_problem
    == "", …)` line in `verify_public` is deleted, and the deploy then stops
    failing a stale page. These tests call `verify_public` over a stub whose
    every other row passes, so the marker record is the only thing that can
    produce a FAIL.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.deploy = load(DEMO / "deploy_demo_public.py",
                          "demo_deploy_marker_wiring_test")

    def run_verify(self, pairs: int) -> tuple[bool, list[str]]:
        stub = _StubTransport(self.deploy, served_page(pairs))
        with mock.patch.object(self.deploy, "http", stub), \
                mock.patch.object(self.deploy, "sign_in",
                                  lambda base, password: "session=stub"):
            return self.deploy.verify_public("x" * 16)

    def test_the_stub_page_with_one_section_passes_every_row(self) -> None:
        ok, lines = self.run_verify(1)
        self.assertTrue(ok, "\n".join(lines))
        self.assertEqual([line for line in lines if line.startswith("FAIL")], [])

    def test_a_page_with_no_section_fails_and_names_the_marker_counts(self) -> None:
        ok, lines = self.run_verify(0)
        self.assertFalse(ok)
        failures = [line for line in lines if line.startswith("FAIL")]
        self.assertEqual(len(failures), 1, "\n".join(lines))
        self.assertIn("exactly one demo-check section", failures[0])
        self.assertIn("0 '<!--demo-check-->' marker(s)", failures[0])
        self.assertIn("0 '<!--/demo-check-->' closer(s)", failures[0])

    def test_a_page_with_two_sections_fails_and_names_the_marker_counts(self) -> None:
        ok, lines = self.run_verify(2)
        self.assertFalse(ok)
        failures = [line for line in lines if line.startswith("FAIL")]
        self.assertEqual(len(failures), 1, "\n".join(lines))
        self.assertIn("2 '<!--demo-check-->' marker(s)", failures[0])
        self.assertIn("2 '<!--/demo-check-->' closer(s)", failures[0])


class DnsRowTests(unittest.TestCase):
    """The two DNS rows take the run's expected address, not the redaction.

    The public tree carries the tenant's redacted address (create_dns.TENANT_IP),
    so the check's default must be that value and a live zone must pass the
    machine's real address explicitly. Both names have to answer the same one.
    The non-redacted address is TEST-NET-2: the leak gate forbids the machine's
    real address in the tree, and a row only compares strings.
    """

    @classmethod
    def setUpClass(cls):
        cls.check = load(DEMO / "check_demo_public.py", "demo_check_dns_test")

    def rows(self, expected: str, demo: list[str], machine: list[str]):
        answers = {self.check.DEMO_NAME: demo, self.check.MACHINE_NAME: machine}
        return self.check.dns_checks(expected, lambda name: list(answers[name]))

    def test_the_tree_default_is_the_redacted_tenant_address(self):
        self.assertEqual(self.check.DEFAULT_DNS, "203.0.113.87")
        rows = self.rows(self.check.DEFAULT_DNS, ["203.0.113.87"], ["203.0.113.87"])
        self.assertEqual([ok for ok, _ in rows], [True, True])
        self.assertIn("203.0.113.87", rows[0][1])

    def test_a_live_zone_passed_explicitly_passes(self):
        rows = self.rows("198.51.100.7", ["198.51.100.7"], ["198.51.100.7"])
        self.assertEqual([ok for ok, _ in rows], [True, True])
        self.assertIn("198.51.100.7", rows[0][1])

    def test_the_live_zone_fails_the_redacted_default(self):
        rows = self.rows(self.check.DEFAULT_DNS, ["198.51.100.7"], ["198.51.100.7"])
        self.assertEqual([ok for ok, _ in rows], [False, False])

    def test_two_names_that_disagree_fail_the_second_row(self):
        rows = self.rows("198.51.100.7", ["198.51.100.7"], ["198.51.100.8"])
        self.assertEqual([ok for ok, _ in rows], [True, False])
        self.assertIn("198.51.100.8", rows[1][1])

    def test_main_passes_its_target_to_the_public_half(self):
        seen: list[str] = []

        def fake_public(password: str, target: str = "") -> str:
            seen.append(target)
            return ""

        original = self.check.public_checks
        self.check.public_checks = fake_public
        self.addCleanup(setattr, self.check, "public_checks", original)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.check.main([]), 0)
            self.assertEqual(self.check.main(["--target", "198.51.100.7"]), 0)
        self.assertEqual(seen, [self.check.DEFAULT_DNS, "198.51.100.7"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
