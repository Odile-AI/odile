"""The demo verifier command — one line, three surfaces, exit 0.

README and ``web/judge.html`` used to hand a judge
``./odile-verify https://demo.odile.fyi``, which exits 2: the pin's ``source``
is HQ, so a demo URL is a tenant run and needs ``--expect``. The demo's
``/verify`` now prints the one-line ``--expect`` command with the measurement
it serves; these tests pin README, the injected section and the tenant
renderer to the same line, and pin the check's extractor to the line a browser
copies.

The release template stays the customer page (ODL-503 finding 1's route: a
demo-only page, not a release re-cut), so every assertion about the installed
template is an assertion about *absence*: a customer machine has no public
demo command.
"""

from __future__ import annotations

import html
import importlib.util
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "deploy" / "demo-host"
COMMAND = DEMO / "verify-command.html"
LEDE = DEMO / "verify-lede.html"
README = ROOT / "README.md"
INSTALLERS = (ROOT / "installer" / "odile_install.py",
              ROOT / "release" / "odile_install.py")
DEMO_URL = "https://demo.odile.fyi"
MEASUREMENT = "9f" * 48
EXPECTED = ("cd odile-verify && ./odile-verify https://demo.odile.fyi"
            " --expect %s" % MEASUREMENT)
COMMAND_SLOT = "./odile-verify https://demo.odile.fyi --expect {{MEASUREMENT}}"


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


def render_page(page: str, measurement: str | None = MEASUREMENT) -> str:
    """Render *page* through the shipped /verify program (t_4fcd465f)."""
    verify = load(ROOT / "verify" / "verify.py", "demo_command_verify_render")
    machine = verify.Machine(root=Path("/"), now=verify.parse_iso("2026-11-14T00:00:00Z"))
    machine.values = {
        "stamp": "2026-11-14T00:00:00Z", "measurement": measurement or "",
        "measurement_short": (measurement or "")[:16],
        "release": "2026.11.2" if measurement else "", "vcpus": "4" if measurement else "",
        "report_fingerprint": "", "fetched_at": "2026-11-14T00:00:00Z" if measurement else "",
        "boot_key": "", "host_key": "", "signing_key": "",
        "state": "available" if measurement else "unavailable",
        "state_text": "" if measurement else "The CPU has not produced a report yet",
    }
    return verify.render(machine, page)


def copied_lines(page: str) -> list[str]:
    """The lines a browser copies out of a <pre><code> block, prompts gone."""
    lines: list[str] = []
    for block in re.finditer(r"<pre[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>", page, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", block.group(1)))
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("$ "):
                line = line[2:].strip()
            if line:
                lines.append(line)
    return lines


class CopyTests(unittest.TestCase):
    """The page a judge reads first.

    ``web/judge.html`` is retired with ``/judge`` (ODL-901: a 301 to
    <https://demo.odile.fyi/login>), and the public README points at the
    demo's own ``/verify`` instead of printing the command (Savio's
    2026-11-14 simplification). So: any demo command that *does* appear in
    the README must carry ``--expect``, and the README must send the reader
    to the page that prints the live line. ``web/judge.html`` is gone with the
    route (ODL-901 deletes it; a deploy from ``main`` keeps the 301).
    """

    def test_every_demo_verifier_command_carries_expect(self):
        for path in (README,):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if "./odile-verify https://demo.odile.fyi" not in line:
                        continue
                    cleaned = line.replace("&lt;", "<").replace("&gt;", ">")
                    rest = cleaned.split("./odile-verify https://demo.odile.fyi", 1)[1]
                    self.assertTrue(
                        rest.strip().startswith("--expect"),
                        f"{path.name}: the demo command has no --expect: {rest!r}")

    def test_the_readme_sends_the_judge_to_verify_for_the_number(self):
        self.assertIn("demo.odile.fyi/verify", README.read_text(encoding="utf-8"))

    def test_the_readme_entry_is_the_demo_login_page(self):
        """`/judge` is retired (ODL-901 answers it with a 301); the README's
        entry must name the page the posted cut prints."""
        text = README.read_text(encoding="utf-8")
        self.assertIn("https://demo.odile.fyi/login", text)
        self.assertNotIn("odile.fyi/judge", text)

    def test_the_readme_names_near_with_deck_verbatim_sentences(self):
        def words(text: str) -> str:
            # A markdown link is a citation, not copy: compare the words.
            return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

        readme = words(README.read_text(encoding="utf-8"))
        deck = words((ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8"))
        for sentence in (
            "Every Odile agent runs on its own AMD SEV-SNP confidential virtual "
            "machine with encrypted memory and NEAR AI confidential model inference "
            "within hardware enclaves, enabling full confidentiality throughout "
            "agentic workflows.",
        ):
            with self.subTest(sentence=sentence[:40]):
                self.assertIn(sentence, readme)
                self.assertIn(sentence, deck)

    def test_no_page_still_prints_the_plain_demo_command_as_a_command(self):
        """A bare URL in prose is fine; a bare URL in a command is the defect."""
        for path in (README,):
            with self.subTest(path=path.name):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if "./odile-verify https://demo.odile.fyi" not in line:
                        continue
                    self.assertIn("--expect", line, line)


class CommandSectionTests(unittest.TestCase):
    """The demo-only section the apply injects into the live template."""

    @classmethod
    def setUpClass(cls):
        cls.apply = load(DEMO / "apply_demo_public.py", "demo_command_section_test")
        cls.text = COMMAND.read_text(encoding="utf-8")

    def test_the_section_is_one_marked_block_with_one_measurement_slot(self):
        self.assertEqual(self.apply.command_problem(self.text), "")
        self.assertEqual(self.text.count("{{MEASUREMENT}}"), 1)
        self.assertIn(COMMAND_SLOT, self.text)
        self.assertNotIn("<script", self.text)
        self.assertNotIn("<img", self.text)

    def test_a_section_without_the_slot_or_the_command_is_refused(self):
        for bad in (
            self.text.replace("{{MEASUREMENT}}", "9f" * 48),
            self.text.replace("--expect {{MEASUREMENT}}", ""),
            self.text.replace("<!--/demo-check-->", ""),
            self.text.replace("<section", "<script"),
        ):
            with self.subTest(bad=bad[:60]):
                self.assertNotEqual(self.apply.command_problem(bad), "")


class WrapTests(unittest.TestCase):
    """The 96-hex ``--expect`` value must wrap, not clip inside the block.

    The page's terminal style is ``pre code{…white-space:pre}``: a 96-hex
    measurement overflows a 710 px (1440) or 352 px (390) block with no
    visible scrollbar, so a stranger reads only its head. The section borrows
    the report's own wrap class; these tests pin the class and the rule
    together, on the page the injector actually reads.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = COMMAND.read_text(encoding="utf-8")
        cls.page = embedded_page(INSTALLERS[0])
        cls.checkout_page = (ROOT / "templates" / "verify.html").read_text(encoding="utf-8")

    def test_the_terminal_block_carries_a_wrap_class_the_page_defines(self):
        block = re.search(r"<pre([^>]*)>", self.text)
        self.assertIsNotNone(block, "the section carries no terminal block")
        classes = re.search(r'class="([^"]+)"', block.group(1))
        self.assertIsNotNone(
            classes, "the terminal block carries no wrap class; --expect clips")
        for page in (self.page, self.checkout_page):
            # The base rule is what clips a plain <pre>; the borrowed class
            # must override it, or the class is decoration.
            self.assertRegex(page, r"pre code\{[^}]*white-space:pre\}")
            for token in classes.group(1).split():
                self.assertRegex(
                    page,
                    r"\." + re.escape(token)
                    + r" code\{[^}]*white-space:pre-wrap[^}]*\}",
                    f"the page defines no wrap rule for .{token} code")

    def test_the_note_names_the_measurement_row_above_it(self):
        notes = re.findall(r'<p class="note">(.*?)</p>', self.text, re.S)
        self.assertTrue(notes, "the section carries no note")
        copies = [html.unescape(re.sub(r"<[^>]+>", "", note)) for note in notes]
        self.assertTrue(any("measurement row above" in copy for copy in copies),
                        copies)
        self.assertFalse(any("shown in the report below" in copy for copy in copies),
                         copies)

    def test_every_class_the_section_uses_is_painted_by_the_page(self):
        """A class the page does not define paints as prose, not as its role.

        The section lands in the release template, so only that sheet paints
        it. A bare ``class="caption"`` label (what this section carried until
        round 4) had no rule in ``templates/verify.html`` and painted at 17 px
        body copy instead of the 9 px micro-label the page it replaces used,
        so the check is the page, not the author's intent.
        """
        page = (ROOT / "templates" / "verify.html").read_text(encoding="utf-8")
        classes: set[str] = set()
        for match in re.finditer(r'class="([^"]+)"', self.text):
            classes.update(match.group(1).split())
        self.assertTrue(classes, "the section carries no class at all")
        for name in sorted(classes):
            with self.subTest(className=name):
                self.assertRegex(
                    page, r"\." + re.escape(name) + r"(?![\w-])",
                    f"the section uses .{name}, which the page never paints")

    def test_the_location_sits_in_the_sentence_not_a_caption(self):
        """The page's convention for a command panel is a `.note` sentence."""
        first = re.search(r"<h2[^>]*>.*?</h2>\s*<p([^>]*)>(.*?)</p>", self.text, re.S)
        self.assertIsNotNone(first, "the section opens with no prose")
        assert first is not None
        self.assertIn('class="note"', first.group(1),
                      "the panel's opening sentence is not a .note")
        copy = html.unescape(re.sub(r"<[^>]+>", "", first.group(2)))
        self.assertIn("from your laptop", copy)
        self.assertIn("PASS or FAIL", copy)
        self.assertNotIn("caption", self.text)


class InjectionTests(unittest.TestCase):
    """The command lands after the machine section, and nowhere near a customer."""

    @classmethod
    def setUpClass(cls):
        cls.apply = load(DEMO / "apply_demo_public.py", "demo_command_inject_test")
        cls.lede = LEDE.read_text(encoding="utf-8").strip()
        cls.command = COMMAND.read_text(encoding="utf-8").strip()

    def demo_page(self, path: Path = INSTALLERS[0]) -> str:
        return self.apply.inject_command(
            self.apply.inject_lede(embedded_page(path), self.lede), self.command)

    def test_the_installed_template_carries_no_demo_command(self):
        for path in INSTALLERS:
            with self.subTest(installer=path.name):
                page = embedded_page(path)
                self.assertNotIn("<!--demo-check-->", page)
                self.assertNotIn(COMMAND_SLOT, page)

    def test_the_tenant_block_keeps_only_the_lede(self):
        injected = self.demo_page()
        block = self.apply.tenant_block(injected)
        self.assertEqual(block.strip(), self.lede)
        self.assertNotIn(self.command, block)

    def test_the_command_follows_the_machine_section_it_names(self):
        """The note names the `measurement` row: it must be above the section."""
        injected = self.demo_page()
        machine = self.apply.MACHINE_SECTION_RE.search(injected)
        self.assertIsNotNone(machine)
        start = injected.index(self.command)
        self.assertGreater(start, machine.end())
        self.assertLess(start, injected.index('id="check"'))
        self.assertLess(injected.index('id="machine"'), start)
        # The section is the only change after the lede: removing it restores
        # the lede-injected page exactly.
        lede_page = self.apply.inject_lede(embedded_page(INSTALLERS[0]), self.lede)
        end = self.apply.MACHINE_SECTION_RE.search(lede_page).end()
        head, _sep, rest = injected.partition(self.command)
        self.assertEqual(head, lede_page[:end] + "\n\n")
        self.assertEqual(rest, lede_page[end:])

    def test_a_page_that_carried_the_command_in_the_tenant_block_converges(self):
        """The demo machine's live page was written before the move."""
        page = embedded_page(INSTALLERS[0])
        old = self.apply.inject_lede(page, self.lede).replace(
            "<!--/tenant-->", self.command + "\n<!--/tenant-->")
        self.assertIn(self.command, self.apply.tenant_block(old))
        self.assertEqual(self.apply.inject_command(old, self.command), self.demo_page())

    def test_the_injection_is_idempotent_and_replaces_a_stale_section(self):
        once = self.demo_page()
        self.assertEqual(self.apply.inject_command(once, self.command), once)
        stale = once.replace("--expect {{MEASUREMENT}}", "--expect stale")
        replaced = self.apply.inject_command(stale, self.command)
        self.assertEqual(replaced, once)
        self.assertNotIn("--expect stale", replaced)

    def test_two_command_sections_fail_loudly(self):
        once = self.demo_page()
        doubled = once.replace("<!--/tenant-->", self.command + "\n<!--/tenant-->")
        with self.assertRaises(ValueError):
            self.apply.inject_command(doubled, self.command)

    def test_a_page_without_the_tenant_block_fails_loudly(self):
        with self.assertRaises(ValueError):
            self.apply.inject_command("<html><body>Verify</body></html>", self.command)

    def test_a_page_without_the_machine_section_fails_loudly(self):
        page = ("<!--tenant-->\n<p class=\"lede\">one</p>\n<!--/tenant-->\n"
                '<section id="check"><h2>Check it yourself</h2></section>\n')
        with self.assertRaises(ValueError):
            self.apply.inject_command(page, self.command)


class RenderTests(unittest.TestCase):
    """What a browser actually reads, through the shipped renderer."""

    @classmethod
    def setUpClass(cls):
        cls.apply = load(DEMO / "apply_demo_public.py", "demo_command_render_test")
        cls.lede = LEDE.read_text(encoding="utf-8").strip()
        cls.command = COMMAND.read_text(encoding="utf-8").strip()

    def rendered(self) -> str:
        page = self.apply.inject_command(
            self.apply.inject_lede(embedded_page(INSTALLERS[0]), self.lede),
            self.command)
        return render_page(page)

    def test_the_command_carries_the_measurement_the_page_serves(self):
        rendered = self.rendered()
        self.assertIn(EXPECTED, copied_lines(rendered))
        self.assertNotIn("{{MEASUREMENT}}", rendered)
        self.assertNotIn("{{", rendered)

    def test_the_command_is_the_only_verifier_line_on_the_page(self):
        lines = [line for line in copied_lines(self.rendered())
                 if "./odile-verify" in line]
        self.assertEqual(lines, [EXPECTED])

    def test_the_command_sits_below_the_machine_section_it_cites(self):
        """The note names the `measurement` row: the section must follow #machine."""
        rendered = self.rendered()
        self.assertLess(rendered.index('<h2 id="machine-h">'),
                        rendered.index("./odile-verify"))
        self.assertLess(rendered.index("./odile-verify"),
                        rendered.index('<h2 id="check-h">'))

    def test_without_a_report_the_page_still_renders_and_names_it(self):
        rendered = render_page(
            self.apply.inject_command(
                self.apply.inject_lede(embedded_page(INSTALLERS[0]), self.lede),
                self.command),
            measurement=None)
        self.assertIn("--expect unavailable", rendered)
        self.assertIn("Report unavailable", rendered)


class CheckTests(unittest.TestCase):
    """The live check reads the exact line a browser copies, then runs it."""

    @classmethod
    def setUpClass(cls):
        cls.check = load(DEMO / "check_demo_public.py", "demo_command_check_test")
        cls.apply = load(DEMO / "apply_demo_public.py", "demo_command_check_apply_test")
        page = cls.apply.inject_command(
            cls.apply.inject_lede(embedded_page(INSTALLERS[0]),
                                  LEDE.read_text(encoding="utf-8").strip()),
            COMMAND.read_text(encoding="utf-8").strip())
        cls.page = render_page(page)

    def test_the_check_extracts_the_line_a_browser_copies(self):
        self.assertEqual(self.check.page_command(self.page), EXPECTED)
        self.assertEqual(self.check.page_command_problem(self.page, MEASUREMENT), "")

    def test_a_plain_demo_command_is_named_as_the_problem(self):
        escaped = EXPECTED.replace("&&", "&amp;&amp;")
        plain = "cd odile-verify &amp;&amp; ./odile-verify https://demo.odile.fyi"
        page = self.page.replace(escaped, plain)
        self.assertEqual(self.check.page_command(page),
                         "cd odile-verify && ./odile-verify https://demo.odile.fyi")
        self.assertIn("--expect", self.check.page_command_problem(page, MEASUREMENT))

    def test_a_page_that_prints_no_command_is_named(self):
        page = self.page.replace(EXPECTED.replace("&&", "&amp;&amp;"),
                                 "curl -sS https://example.test")
        self.assertEqual(self.check.page_command(page), "")
        self.assertIn("no", self.check.page_command_problem(page, MEASUREMENT))

    def test_a_page_that_prints_two_commands_is_refused(self):
        escaped = EXPECTED.replace("&&", "&amp;&amp;")
        page = self.page.replace(escaped, escaped + "\n" + escaped)
        self.assertEqual(self.check.page_command(page), "")

    def test_the_runner_refuses_a_line_that_is_not_the_pages(self):
        for bad in ("odile-verify https://demo.odile.fyi --expect " + MEASUREMENT,
                    "cd odile-verify && ./odile-verify https://demo.odile.fyi",
                    "cd x && rm -rf / && ./odile-verify https://demo.odile.fyi --expect "
                    + MEASUREMENT):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.check.command_script(bad)


class ReportTests(unittest.TestCase):
    """`apply --report` says whether both demo writes are on the page."""

    def setUp(self):
        self.apply = load(DEMO / "apply_demo_public.py", "demo_command_report_test")
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-demo-command-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.page_path = self.tmp / "verify.html"
        shutil.copyfile(LEDE, self.tmp / LEDE.name)
        shutil.copyfile(COMMAND, self.tmp / COMMAND.name)
        self.apply.VERIFY_TEMPLATE = self.page_path
        self.apply.VERIFY_BACKUP = self.tmp / "verify.html.release"
        self.apply.__file__ = str(self.tmp / "apply_demo_public.py")
        lede = LEDE.read_text(encoding="utf-8").strip()
        command = COMMAND.read_text(encoding="utf-8").strip()
        page = embedded_page(INSTALLERS[0])
        self.page_path.write_text(
            self.apply.inject_command(self.apply.inject_lede(page, lede), command),
            encoding="utf-8")

    def test_the_report_sees_both_writes(self):
        state = self.apply.verify_state()
        self.assertTrue(state["demo_lede"])
        self.assertTrue(state["demo_command"])
        self.assertFalse(state["customer_lede"])

    def test_a_page_with_the_lede_only_reports_no_command(self):
        page = self.apply.inject_lede(embedded_page(INSTALLERS[0]),
                                      LEDE.read_text(encoding="utf-8").strip())
        self.page_path.write_text(page, encoding="utf-8")
        state = self.apply.verify_state()
        self.assertTrue(state["demo_lede"])
        self.assertFalse(state["demo_command"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
