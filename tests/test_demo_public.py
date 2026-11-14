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

import hashlib
import importlib.util
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

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
    match = re.search(r"VERIFY_PAGE = r'''(.*?)'''", text, re.S)
    assert match is not None, f"{path} carries no VERIFY_PAGE"
    return match.group(1)


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
        self.assertIn("every judge who signs in lands in the same workspace", text)
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
        cls.first_visit = load(ROOT / "installer" / "first_visit.py", "demo_first_visit_test")
        cls.lede = LEDE.read_text(encoding="utf-8").strip()
        cls.page = embedded_page(INSTALLERS[0])
        cls.report = {"state": "ok", "at": "2026-11-14T00:00:00Z", "text": "report body"}
        cls.attestation = {"measurement": "9f" * 48, "fetched_at": "2026-11-14T00:00:00Z",
                           "meta": "version 3, 1184 bytes"}

    def render(self, page: str) -> str:
        return self.first_visit.render_verify_page(page, self.report, self.attestation)

    def test_a_customer_page_renders_the_general_row(self):
        rendered = self.render(self.page)
        self.assertIn(CUSTOMER_SENTENCE, rendered)
        self.assertNotIn(self.lede, rendered)

    def test_the_demo_page_renders_the_demo_row_and_keeps_the_live_report(self):
        rendered = self.render(self.apply.inject_lede(self.page, self.lede))
        first = re.search(r'<p class="lede">.*?</p>', rendered, re.S)
        self.assertIsNotNone(first)
        self.assertEqual(first.group(0), self.lede)
        self.assertNotIn(CUSTOMER_SENTENCE, rendered)
        self.assertIn(self.attestation["measurement"], rendered)
        self.assertIn(self.report["text"], rendered)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
