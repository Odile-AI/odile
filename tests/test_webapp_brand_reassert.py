"""ODL-503 follow-up: the Webapp brand survives a rebuild.

`hermes update` and `hermes webapp --build-only` rebuild
`apps/desktop/dist-webapp` from the vendor package. `index.html` becomes the
vendor's shell again and `odile-brand.js` is gone with it: the installer's
`ensure_webapp_brand()` is a build-time patch with nothing to re-run it.

These tests cover the re-assert that is shipped instead:

  * `deploy/image/odile-webapp-brand.py` — the same splice as the installer's,
    from the same bytes, idempotent (`same` on a second run) and
    owner-preserving;
  * `odile-webapp-brand.path` + `.service` — the bundle changing starts the
    oneshot;
  * the drop-in that runs the same helper before the gateway starts;
  * the three image builders, so a fresh machine carries all of it.

The image builds are not run here (they need debootstrap and minutes); the
packaging tests are the gate that a builder cannot silently forget a file.
"""

import contextlib
import importlib.util
import io
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = ROOT / "deploy" / "image"
HELPER = IMAGE / "odile-webapp-brand.py"
INSTALLER = ROOT / "installer" / "odile_install.py"
TEMPLATE = ROOT / "templates" / "webapp-brand.js"

VENDOR_SHELL = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    "  <head>\n"
    '    <meta charset="utf-8">\n'
    "    <title>Hermes</title>\n"
    "  </head>\n"
    "  <body>\n"
    '    <div id="root"></div>\n'
    "  </body>\n"
    "</html>\n"
)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class HelperSpliceTests(unittest.TestCase):
    """The helper's splice is the installer's splice, and it converges."""

    @classmethod
    def setUpClass(cls):
        cls.helper = load_module(HELPER, "odile_webapp_brand")
        cls.installer = load_module(INSTALLER, "odile_installer_for_brand")

    def test_the_image_ships_the_template_bytes(self):
        self.assertEqual((IMAGE / "webapp-brand.js").read_bytes(), TEMPLATE.read_bytes(),
                         "deploy/image/webapp-brand.js must be templates/webapp-brand.js "
                         "byte for byte; the tag's ?v= is its sha256")

    def test_the_tag_is_the_installer_tag(self):
        version = self.helper.brand_version(self.installer.BRAND_JS.encode())
        self.assertEqual(version, self.installer.brand_version(self.installer.BRAND_JS.encode()))
        self.assertEqual(self.helper.brand_tag_lines(version),
                         self.installer.brand_tag_lines(version),
                         "one tag format, or a re-assert and a re-install disagree")

    def test_the_splice_matches_the_installer_on_a_real_shell(self):
        for shell in (VENDOR_SHELL,
                      "<!doctype html>\n<html>\n<head>\n<title>x</title>\n</head>\n"
                      "<body></body>\n</html>\n",
                      VENDOR_SHELL.replace("  <head>\n", "  <head>\n    <base href=\"/\">\n")):
            self.assertEqual(self.helper.with_brand(shell, "abc123"),
                             self.installer.with_brand(shell, "abc123"))
            self.assertEqual(self.helper.strip_brand(self.helper.with_brand(shell, "abc123")),
                             self.installer.strip_brand(shell))

    def test_a_rebuilt_shell_gains_exactly_one_tag_and_the_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "index.html"
            bundle.write_text(VENDOR_SHELL, encoding="utf-8")
            source = Path(tmp) / "webapp-brand.js"
            source.write_bytes(TEMPLATE.read_bytes())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                changed = self.helper.apply(bundle, source)
            self.assertTrue(changed)
            text = bundle.read_text(encoding="utf-8")
            self.assertEqual(text.count("odile-brand.js"), 1)
            self.assertIn(f"odile-brand.js?v={self.helper.brand_version(TEMPLATE.read_bytes())}",
                          text)
            asset = bundle.parent / "odile-brand.js"
            self.assertEqual(asset.read_bytes(), TEMPLATE.read_bytes())
            self.assertEqual(stat.S_IMODE(asset.stat().st_mode), 0o644)
            self.assertIn("write", output.getvalue())
            # ... and the second run is the proof the card names.
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                changed = self.helper.apply(bundle, source)
            self.assertFalse(changed)
            self.assertIn("same", output.getvalue())
            self.assertEqual(bundle.read_text(encoding="utf-8").count("odile-brand.js"), 1)

    def test_the_owner_of_the_bundle_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "index.html"
            bundle.write_text(VENDOR_SHELL, encoding="utf-8")
            source = Path(tmp) / "webapp-brand.js"
            source.write_bytes(TEMPLATE.read_bytes())
            if os.getuid() == 0:
                os.chown(bundle, 12345, 12345)
            with contextlib.redirect_stdout(io.StringIO()):
                self.helper.apply(bundle, source)
            st = bundle.stat()
            asset = (bundle.parent / "odile-brand.js").stat()
            if os.getuid() == 0:
                self.assertEqual((st.st_uid, st.st_gid), (12345, 12345))
                self.assertEqual((asset.st_uid, asset.st_gid), (12345, 12345))
            else:
                self.assertEqual(st.st_uid, os.getuid())
                self.assertEqual(asset.st_uid, os.getuid())

    def test_a_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "index.html"
            bundle.write_text(VENDOR_SHELL, encoding="utf-8")
            source = Path(tmp) / "webapp-brand.js"
            source.write_bytes(TEMPLATE.read_bytes())
            before = (bundle.read_text(encoding="utf-8"), bundle.stat().st_mtime_ns)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                changed = self.helper.apply(bundle, source, dry=True)
            self.assertTrue(changed)
            self.assertIn("would write", output.getvalue())
            self.assertFalse((bundle.parent / "odile-brand.js").exists())
            self.assertEqual(bundle.read_text(encoding="utf-8"), before[0])
            self.assertEqual(bundle.stat().st_mtime_ns, before[1])

    def test_a_missing_bundle_is_a_wait_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "dist-webapp" / "index.html"
            source = Path(tmp) / "webapp-brand.js"
            source.write_bytes(TEMPLATE.read_bytes())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                changed = self.helper.apply(bundle, source)
            self.assertFalse(changed)
            self.assertIn("wait", output.getvalue())
            self.assertFalse(bundle.parent.exists(), "a missing bundle is not created")

    def test_an_incomplete_shell_is_left_alone(self):
        for shell in ("<html><body>half a build", "<!doctype html>\n<html>\n<body></body>\n</html>\n"):
            with tempfile.TemporaryDirectory() as tmp:
                bundle = Path(tmp) / "index.html"
                bundle.write_text(shell, encoding="utf-8")
                source = Path(tmp) / "webapp-brand.js"
                source.write_bytes(TEMPLATE.read_bytes())
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    changed = self.helper.apply(bundle, source)
                self.assertFalse(changed, shell)
                self.assertEqual(bundle.read_text(encoding="utf-8"), shell)
                self.assertIn("wait", output.getvalue())

    def test_a_shell_still_being_written_is_read_after_it_settles(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "index.html"
            bundle.write_text("<html><head>\n", encoding="utf-8")
            calls = []

            def sleep(_seconds):
                calls.append(_seconds)
                if len(calls) == 1:
                    with bundle.open("a", encoding="utf-8") as handle:
                        handle.write("<title>x</title></head><body></body></html>\n")

            data = self.helper.read_stable(bundle, sleep=sleep)
            self.assertIn(b"</html>", data)
            self.assertEqual(bundle.read_bytes(), data, "the second read is the settled one")

    def test_running_the_helper_twice_in_a_process_prints_same(self):
        result = subprocess.run([sys.executable, str(HELPER), "--self-test"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)


class HelperUnionTests(unittest.TestCase):
    """The re-assert this branch deploys carries all three splices (ODL-505).

    The demo host had the union helper installed (brand + chrome + tab icons),
    and a deploy from a branch that predates it reverted the tab at 13:11Z.
    This branch carries the same bytes ODL-505 ships, so the next deploy keeps
    name, chrome and tab together.
    """

    def test_the_helper_carries_brand_chrome_and_icons(self):
        helper = load_module(HELPER, "odile_webapp_brand_union")
        for name in ("with_brand", "with_chrome", "with_icons",
                     "ICON_ASSETS", "ICON_LINK_LINES", "icon_files"):
            self.assertTrue(hasattr(helper, name),
                            f"the helper lost {name}; a deploy from this branch "
                            "would revert the tab or the chrome")
        self.assertIn("--icons", HELPER.read_text(encoding="utf-8"))


class UnitFileTests(unittest.TestCase):
    """The trigger is the bundle changing; the boot is not the trigger."""

    def setUp(self):
        self.path_unit = (IMAGE / "odile-webapp-brand.path").read_text(encoding="utf-8")
        self.service = (IMAGE / "odile-webapp-brand.service").read_text(encoding="utf-8")

    def test_the_path_unit_watches_the_built_shell(self):
        self.assertIn(
            "PathChanged=__TENANT_HOME__/.hermes/hermes-agent/apps/desktop/dist-webapp/index.html",
            self.path_unit)
        for asset in ("odile-brand.js", "odile-demo-chrome.css", "odile-demo-chrome.js"):
            self.assertIn(
                "PathChanged=__TENANT_HOME__/.hermes/hermes-agent/apps/desktop/dist-webapp/"
                + asset, self.path_unit, f"the path unit does not watch {asset}")
        self.assertIn("Unit=odile-webapp-brand.service", self.path_unit)
        self.assertIn("WantedBy=multi-user.target", self.path_unit)

    def test_the_service_is_a_oneshot_that_runs_the_helper(self):
        self.assertIn("Type=oneshot", self.service)
        self.assertIn("/usr/bin/python3 /usr/local/lib/odile/odile-webapp-brand.py", self.service)
        self.assertIn("ReadWritePaths=__TENANT_HOME__", self.service)
        self.assertNotIn("[Install]", self.service,
                         "the service runs on a change, not at boot")

    def test_the_dropin_reasserts_before_the_gateway_starts(self):
        dropin = (IMAGE / "odile-webapp-brand.conf").read_text(encoding="utf-8")
        self.assertIn("ExecStartPre=/usr/bin/python3 "
                      "/usr/local/lib/odile/odile-webapp-brand.py", dropin)
        self.assertIn("--quiet", dropin)


class ImagePackagingTests(unittest.TestCase):
    """A fresh machine must carry the re-assert, not only the demo tenant."""

    FILES = ("odile-webapp-brand.py", "webapp-brand.js",
             "webapp-chrome.css", "webapp-chrome.js",
             "odile-webapp-brand.service", "odile-webapp-brand.path",
             "odile-webapp-brand.conf")

    def test_every_image_builder_carries_the_new_files(self):
        for name in ("Containerfile", "mkdisk.sh", "sealed-image.sh"):
            text = (IMAGE / name).read_text(encoding="utf-8")
            for wanted in self.FILES:
                self.assertIn(wanted, text, f"{name} does not carry {wanted}")
        installer_script = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        for wanted in self.FILES:
            self.assertIn(wanted, installer_script, f"install-stack.sh does not carry {wanted}")

    def test_install_stack_enables_the_watch_and_converges_the_build(self):
        text = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        self.assertIn("systemctl enable odile-webapp-brand.path", text)
        self.assertIn('--home "$TENANT_HOME"', text)
        self.assertIn("odile-webapp-brand.py", text)


CHROME_CSS = IMAGE / "webapp-chrome.css"
CHROME_JS = IMAGE / "webapp-chrome.js"
DEMO_HOST = ROOT / "deploy" / "demo-host"


def declarations(css: str, selector: str) -> dict[str, list[str]]:
    """Every declaration of every rule whose selector list contains `selector`."""
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found: dict[str, list[str]] = {}
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", clean):
        selectors = [part.strip() for part in match.group(1).split(",")]
        if selector not in selectors:
            continue
        for chunk in match.group(2).split(";"):
            if ":" not in chunk:
                continue
            name, value = chunk.split(":", 1)
            found.setdefault(name.strip(), []).append(value.strip())
    return found


def block_after(text: str, marker: str) -> str:
    """The balanced `{...}` block whose opening follows `marker`."""
    start = text.find(marker)
    if start < 0:
        return ""
    opening = text.find("{", start + len(marker))
    if opening < 0:
        return ""
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1:index]
    return ""


class ChromeParityTests(unittest.TestCase):
    """The bytes that ship are the ODL-330/ODL-500 source, not a third copy."""

    def test_the_image_copy_is_the_demo_host_source(self):
        for name in ("webapp-chrome.css", "webapp-chrome.js"):
            source = DEMO_HOST / name
            self.assertTrue(source.is_file(), f"{source} is the ODL-500 source of record")
            self.assertEqual((IMAGE / name).read_bytes(), source.read_bytes(),
                             f"deploy/image/{name} is a fork of the demo-host source")

    def test_the_shipped_stylesheet_has_one_embedded_face(self):
        css = CHROME_CSS.read_text(encoding="utf-8")
        self.assertEqual(css.count("data:font/woff2;base64,"), 1,
                         "the Cormorant Garamond subset is embedded once and only once")
        self.assertNotIn('"Odile Cormorant"', css,
                         "the embedded face is Cormorant Garamond, the sheet's family")


class SurfaceValueTests(unittest.TestCase):
    """Every value the card certifies, pinned against drift."""

    @classmethod
    def setUpClass(cls):
        cls.css = CHROME_CSS.read_text(encoding="utf-8")
        cls.js = CHROME_JS.read_text(encoding="utf-8")

    def test_the_palette_is_the_sheet(self):
        root = declarations(self.css, ":root")
        expected = {
            "--theme-foreground": "#1c1813",
            "--theme-background-seed": "#eadfc4",
            "--theme-sidebar-seed": "#eadfc4",
            "--theme-card-seed": "#f2e9d4",
            "--theme-elevated-seed": "#f2e9d4",
            "--dt-muted": "#ddd0ae",
            "--ui-text-secondary": "#4a4136",
            "--dt-destructive": "#6e2a2a",
            "--dt-border": "rgba(28, 24, 19, 0.18)",
        }
        for token, value in expected.items():
            values = root.get(token, [])
            self.assertTrue(any(value in declaration for declaration in values),
                            f"{token} drifted from BRAND.md: {values!r}")

    def test_the_page_paints_the_sheet_paper_at_both_roots(self):
        for selector in ("html", "body"):
            self.assertIn("#eadfc4", " ".join(declarations(self.css, selector)
                                              .get("background-color", [])),
                          f"{selector} does not paint --paper")

    def test_the_face_is_cormorant_garamond_first(self):
        face = block_after(self.css, "@font-face")
        self.assertIn('font-family: "Cormorant Garamond"', face)
        for rule in ("html p.wordmark", "html p.wordmark + p", "a.odile-verify-link"):
            family = " ".join(declarations(self.css, rule).get("font-family", []))
            self.assertTrue(family.startswith('"Cormorant Garamond"'),
                            f"{rule} does not name Cormorant Garamond first: {family!r}")
        self.assertTrue(" ".join(declarations(self.css, ":root")
                                 .get("--dt-font-sans", []))
                        .startswith('"Cormorant Garamond"'))

    def test_focus_is_two_px_ink_at_three_px_offset(self):
        ring = declarations(self.css, ":focus-visible")
        self.assertEqual(ring.get("outline"), ["2px solid #1c1813 !important"])
        self.assertEqual(ring.get("outline-offset"), ["3px !important"])

    def test_reduced_motion_removes_the_vendor_animations(self):
        media = block_after(self.css, "@media (prefers-reduced-motion: reduce)")
        self.assertTrue(media, "no reduced-motion block")
        for selector in ('[class*="glyph-spinner"]', '[class*="decode-"]'):
            self.assertIn(selector, media)
        self.assertIn("animation: none !important", media)

    def test_the_verify_link_is_one_44px_target_with_the_full_name(self):
        link = declarations(self.css, "a.odile-verify-link")
        self.assertEqual(link.get("height"), ["44px"])
        self.assertIn("VERIFY_LABEL = 'Verify this machine'", self.js)
        self.assertIn("document.createTextNode('Verify')", self.js)
        self.assertIn("more.textContent = ' this machine'", self.js)
        narrow = block_after(self.css, "@media (max-width: 560px)")
        self.assertIn("a.odile-verify-link .odile-verify-more", narrow)
        self.assertIn("display: none", narrow)

    def test_demo_only_copy_is_gated_on_the_demo_host(self):
        self.assertIn("const DEMO_HOST = 'demo.odile.fyi'", self.js)
        self.assertIn("const IS_DEMO = location.hostname === DEMO_HOST", self.js)
        self.assertIn("if (IS_DEMO && lede", self.js)

    def test_the_session_menu_is_left_to_the_vendor(self):
        # ODL-391 §2.5 retired with session_guard.py and Caddy's REST refusal
        # (t_69f1a5f3, t_ee5aac66). Delete / Rename… / Archive work from the
        # judge's browser; a hide here is re-installed on the next deploy.
        for gone in ("hideRowWrites", "collapseSeparators", "navigateMenu",
                     "ROW_WRITES", "SESSION_MENU", "SESSION_TRIGGER", "MENU_KEYS"):
            self.assertNotIn(gone, self.js,
                             f"{gone} is back; the guard it was written for is gone")
        self.assertIn("const MENU_ITEM = '[role=\"menuitem\"]'", self.js)

    def test_the_composer_fill_is_the_card_on_the_vendors_own_scope(self):
        # The vendor re-declares --composer-fill on [data-slot="composer-root"];
        # a value at :root is only inherited and loses to that declaration, so
        # the sheet's card never painted (measured #F8F5ED on the live tenant).
        scope = declarations(self.css, '[data-slot="composer-root"]')
        self.assertEqual(scope.get("--composer-fill"), ["#f2e9d4 !important"],
                         "the composer's fill must be re-declared on the vendor's "
                         "own scope, or its translucent mix is the painted value")
        self.assertIn("#f2e9d4", " ".join(declarations(self.css, ":root")
                                          .get("--composer-fill", [])))

    def test_the_composer_placeholder_is_the_sheet_placeholder(self):
        for selector in ('html [data-slot="composer-rich-input"]:empty::before',
                         'html [data-slot="composer-rich-input"][data-empty]::before'):
            rule = declarations(self.css, selector)
            self.assertEqual(rule.get("color"), ["#5d5244 !important"],
                             f"{selector} does not carry --placeholder-on-paper")
            self.assertEqual(rule.get("opacity"), ["1 !important"],
                             f"{selector} still composites the vendor's 54 % ink")

    def test_the_sidebar_label_is_soft(self):
        self.assertEqual(declarations(self.css, ":root").get("--ui-text-quaternary"),
                         ["#4a4136 !important"],
                         "the label role is --soft, not soft at 72 % opacity")
        self.assertNotIn("rgba(74, 65, 54, 0.72)", self.css)

    def test_the_verify_link_is_the_demos_here_and_the_machines_elsewhere(self):
        # The demo keeps the ODL-500 href. A subscriber's report is served by
        # the machine at /verify (odilectl/offboard.py probes it); sending a
        # subscriber to odile.fyi/verify says "This is Odile's demo machine".
        self.assertIn("const VERIFY_HREF_DEMO = 'https://odile.fyi/verify'", self.js)
        self.assertIn("const VERIFY_HREF_TENANT = '/verify'", self.js)
        self.assertIn("link.href = IS_DEMO ? VERIFY_HREF_DEMO : VERIFY_HREF_TENANT",
                      self.js)

    def test_the_verify_box_starts_where_its_ring_is_whole(self):
        link = declarations(self.css, "a.odile-verify-link")
        self.assertEqual(link.get("top"), ["5px"])
        self.assertEqual(link.get("height"), ["44px"], "the 44 px target stays")
        self.assertIn("const top = 5", self.js)
        self.assertIn("link.style.top = `${top}px`", self.js)
        ring = declarations(self.css, ":focus-visible")
        self.assertEqual(ring.get("outline"), ["2px solid #1c1813 !important"])
        top = int(link["top"][0].removesuffix("px"))
        offset = int(ring["outline-offset"][0].split()[0].removesuffix("px"))
        self.assertGreaterEqual(top - offset - 2, 0,
                                "the 2 px ring at 3 px offset is clipped at the viewport top")

    def test_the_workspace_has_one_heading_and_a_main_landmark(self):
        self.assertIn("const CHAT_SURFACE = '[data-chat-surface]'", self.js)
        self.assertIn("surface.setAttribute('role', 'main')", self.js)
        self.assertIn("document.createElement('h1')", self.js)
        self.assertIn("heading.className = 'odile-workspace-title'", self.js)
        hidden = declarations(self.css, "h1.odile-workspace-title")
        self.assertEqual(hidden.get("position"), ["absolute !important"])
        self.assertIn("clip-path", hidden)

    def test_the_composer_is_body_text(self):
        rule = declarations(self.css, 'html [data-slot="composer-rich-input"]')
        self.assertEqual(rule.get("font-size"), ["17px !important"],
                         "the composer is product prose, not the vendor's 14.3 px scale")
        self.assertEqual(rule.get("line-height"), ["1.55 !important"])


class ChromeSpliceTests(unittest.TestCase):
    """One tag per asset, one convergence, the vendor shell underneath."""

    @classmethod
    def setUpClass(cls):
        cls.helper = load_module(HELPER, "odile_webapp_brand_chrome")

    def sources(self, root: Path):
        copies = []
        for name in ("webapp-chrome.css", "webapp-chrome.js"):
            copy = root / name
            copy.write_bytes((IMAGE / name).read_bytes())
            copies.append(copy)
        return copies

    def test_a_rebuilt_shell_gains_both_assets_and_one_tag_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "index.html"
            bundle.write_text(VENDOR_SHELL, encoding="utf-8")
            brand = root / "webapp-brand.js"
            brand.write_bytes(TEMPLATE.read_bytes())
            sources = self.sources(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                changed = self.helper.apply(bundle, brand, sources)
            self.assertTrue(changed)
            text = bundle.read_text(encoding="utf-8")
            self.assertEqual(text.count("odile-brand.js"), 1)
            self.assertEqual(text.count("odile-demo-chrome.js"), 1)
            self.assertEqual(text.count("odile-demo-chrome.css"), 1)
            self.assertEqual((root / "odile-demo-chrome.css").read_bytes(),
                             (IMAGE / "webapp-chrome.css").read_bytes())
            self.assertEqual((root / "odile-demo-chrome.js").read_bytes(),
                             (IMAGE / "webapp-chrome.js").read_bytes())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                changed = self.helper.apply(bundle, brand, sources)
            self.assertFalse(changed)
            self.assertIn("same", output.getvalue())

    def test_stripping_both_blocks_restores_the_vendor_shell(self):
        version = self.helper.brand_version(TEMPLATE.read_bytes())
        combined = self.helper.chrome_version([CHROME_CSS.read_bytes(),
                                               CHROME_JS.read_bytes()])
        surface = self.helper.with_chrome(
            self.helper.with_brand(VENDOR_SHELL, version), combined)
        self.assertEqual(self.helper.strip_chrome(self.helper.strip_brand(surface)),
                         VENDOR_SHELL)

    def test_a_rebuild_that_drops_the_chrome_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "index.html"
            bundle.write_text(VENDOR_SHELL, encoding="utf-8")
            brand = root / "webapp-brand.js"
            brand.write_bytes(TEMPLATE.read_bytes())
            sources = self.sources(root)
            with contextlib.redirect_stdout(io.StringIO()):
                self.helper.apply(bundle, brand, sources)
            # The gateway's own rewrite: the vendor shell plus the brand block,
            # without the chrome (measured 3,058 B on the demo tenant).
            rewritten = self.helper.with_brand(
                VENDOR_SHELL, self.helper.brand_version(TEMPLATE.read_bytes()))
            bundle.write_text(rewritten, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                changed = self.helper.apply(bundle, brand, sources)
            self.assertTrue(changed)
            text = bundle.read_text(encoding="utf-8")
            self.assertEqual(text.count("odile-demo-chrome.css"), 1)
            self.assertEqual(text.count("odile-demo-chrome.js"), 1)
            self.assertEqual(text.count("odile-brand.js"), 1)


if __name__ == "__main__":
    unittest.main()
