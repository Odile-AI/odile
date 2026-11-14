"""The sign-in page a judge meets first — the live demo's only static file.

The page is `deploy/demo-host/login.html`; `apply_demo_public.py` installs it
on the tenant as `/srv/odile-demo/login.html` behind Caddy's `handle /login*`.
Until t_9d7ee536 these checks lived in `tests/test_demo_host.py` beside the HQ
demo's deployer; the page outlived the HQ demo, so the checks moved here with
it, pointed at the installer that still ships it (ODL-390, t_3d717ffc,
t_330cea5c, t_9896a577).
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import os
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGIN_PAGE = ROOT / "deploy" / "demo-host" / "login.html"
CLAIMS_MD = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
README_MD = (ROOT / "README.md").read_text(encoding="utf-8")
LOGIN_HTML = LOGIN_PAGE.read_text(encoding="utf-8")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apply = load("odile_apply_demo_public", ROOT / "deploy" / "demo-host" / "apply_demo_public.py")
deploy = load("odile_deploy_demo_public", ROOT / "deploy" / "demo-host" / "deploy_demo_public.py")

# Savio's boundary sentence, 2026-11-13 (card t_3d717ffc), verbatim. "access is
# open to public" and "Everyone could see" are his words; they are not a typo to
# fix without asking on the card.
BOUNDARY_SENTENCE = (
    "This is an Odile demo instance. Despite that it runs on confidential compute "
    "and inference, access is open to public. Everyone could see everything on this "
    "machine. Refrain from uploading personal and sensitive details."
)


def readme_credential(field: str) -> str:
    """The published demo account, read from README's `| Field | Value |` row."""
    values = re.findall(rf"^\| {field} \| `([^`]+)` \|$", README_MD, re.MULTILINE)
    if not values:
        raise AssertionError(f"README.md has no '| {field} | `…` |' row")
    if len(set(values)) != 1:
        raise AssertionError(f"README.md's {field} rows disagree: {values}")
    return values[0]


class LoginPageTests(unittest.TestCase):
    """ODL-390 and t_3d717ffc: one button, Savio's sentence, no version credit."""

    def test_the_title_and_wordmark_are_odiles(self) -> None:
        self.assertIn("<title>Sign in — Odile</title>", LOGIN_HTML)
        self.assertIn('<p class="wordmark">Odile</p>', LOGIN_HTML)
        self.assertNotIn("Hermes Agent dashboard", LOGIN_HTML)
        self.assertNotIn("Nous", LOGIN_HTML)
        self.assertIn('rel="icon"', LOGIN_HTML)
        self.assertIn("r='7.5'", LOGIN_HTML)
        self.assertIn("stroke-width='1.6'", LOGIN_HTML)

    def test_the_boundary_sentence_is_the_copy(self) -> None:
        self.assertIn(BOUNDARY_SENTENCE, LOGIN_HTML)
        # The sentences the card removes, and the version credit with them.
        for gone in (
            "Runs on Hermes",
            "public by design",
            "nightly reset",
            "Every judge sees these sessions and files.",
            "resets daily at 00:00",
        ):
            self.assertNotIn(gone, LOGIN_HTML)

    def test_the_copy_is_a_row_in_the_claims_deck(self) -> None:
        # Two-sided: the page cannot carry a sentence the deck does not.
        self.assertIn(BOUNDARY_SENTENCE, CLAIMS_MD)

    def test_the_form_is_hermes_password_login(self) -> None:
        # The auth mechanism is Hermes's: the page only presents it.
        self.assertIn('data-provider="basic"', LOGIN_HTML)
        self.assertIn("fetch('/auth/password-login'", LOGIN_HTML)
        self.assertIn("credentials: 'same-origin'", LOGIN_HTML)
        self.assertIn(
            "var next = new URLSearchParams(window.location.search).get('next') || '';",
            LOGIN_HTML,
        )
        self.assertIn("next: next", LOGIN_HTML)
        self.assertIn("window.location.assign((data && data.next) || '/'", LOGIN_HTML)

    def test_one_h1_one_button_and_no_fields(self) -> None:
        self.assertEqual(LOGIN_HTML.count("<h1"), 1)
        self.assertEqual(LOGIN_HTML.count("<input"), 0)
        self.assertEqual(LOGIN_HTML.count("<label"), 0)
        self.assertEqual(LOGIN_HTML.count("<button"), 1)
        self.assertEqual(LOGIN_HTML.count('type="submit"'), 1)
        self.assertIn('<button type="submit" aria-label="Sign in">Sign in</button>', LOGIN_HTML)
        self.assertIn('role="alert"', LOGIN_HTML)

    def test_the_button_signs_in_with_the_published_demo_account(self) -> None:
        # One source of truth: a rotation in README.md that misses the page
        # would leave a button that cannot sign in.
        password = readme_credential("Password")
        username = readme_credential("Username")
        pw_literal = re.search(r"var DEMO_PASSWORD = '([^']*)';", LOGIN_HTML)
        user_literal = re.search(r"var DEMO_USERNAME = '([^']*)';", LOGIN_HTML)
        self.assertIsNotNone(pw_literal)
        self.assertIsNotNone(user_literal)
        assert pw_literal is not None and user_literal is not None
        self.assertEqual(pw_literal.group(1), password)
        self.assertEqual(user_literal.group(1), username)
        self.assertIn("password: DEMO_PASSWORD", LOGIN_HTML)

    def test_the_error_copy_names_nothing_the_judge_can_change(self) -> None:
        # With no fields, "invalid username or password" names nothing the
        # judge can act on; the 429 sentence is kept.
        self.assertIn("'Sign-in failed. Please try again.'", LOGIN_HTML)
        self.assertIn("'Too many attempts. Please wait and try again.'", LOGIN_HTML)
        self.assertNotIn("Invalid username or password", LOGIN_HTML)

    def test_focus_is_visible_and_reduced_motion_is_honoured(self) -> None:
        self.assertIn("outline:2px solid var(--ink)", LOGIN_HTML)
        self.assertIn("outline-offset:3px", LOGIN_HTML)
        self.assertIn("outline:2px solid Highlight", LOGIN_HTML)
        self.assertIn("prefers-reduced-motion: reduce", LOGIN_HTML)
        self.assertNotIn("outline:none", LOGIN_HTML)
        self.assertIn("button.busy::after", LOGIN_HTML)
        self.assertIn("transform:scaleX(.5)", LOGIN_HTML)

    def test_the_dead_field_and_credit_css_is_gone(self) -> None:
        self.assertNotIn("input", LOGIN_HTML)
        for gone in (".field", "label{", ".help", ".notice", ".credit", "pw-help"):
            self.assertNotIn(gone, LOGIN_HTML)

    def test_the_install_markers_describe_this_page(self) -> None:
        # Both the uploader (HQ) and the installer (tenant) refuse a page that
        # lost a marker, and the two tuples must stay the same page's.
        self.assertEqual(tuple(apply.LOGIN_MARKERS), tuple(deploy.LOGIN_MARKERS))
        for marker in apply.LOGIN_MARKERS:
            self.assertIn(marker, LOGIN_HTML)
        joined = " ".join(apply.LOGIN_MARKERS)
        self.assertNotIn("Hermes", joined)
        self.assertNotIn("demo machine", joined)

    def test_the_font_is_embedded_and_nothing_is_external(self) -> None:
        self.assertIn("data:font/woff2;base64,", LOGIN_HTML)
        self.assertIn('font-family:"Cormorant Garamond"', LOGIN_HTML)
        self.assertNotIn('src="http', LOGIN_HTML)
        self.assertNotIn('<link rel="stylesheet" href="http', LOGIN_HTML)
        self.assertNotIn('<link rel="stylesheet"', LOGIN_HTML)
        # One button and one quiet link: the proof is the only second way out.
        self.assertEqual(LOGIN_HTML.count("<a "), 1)
        self.assertIn('<a href="/verify">', LOGIN_HTML)

    def test_the_plate_is_one_inline_decorative_image(self) -> None:
        # BRAND.md §Product surfaces: the sign-in carries one contained plate
        # band of the same Ruysch, inlined as a data URI because the demo serves
        # the page from one file and has no static route for the painting. One
        # artwork, no alt text, the exact shipped bytes.
        self.assertEqual(LOGIN_HTML.count("<img "), 1)
        self.assertIn('class="plate" src="data:image/webp;base64,', LOGIN_HTML)
        self.assertIn('alt=""', LOGIN_HTML)
        self.assertIn('width="1400" height="875"', LOGIN_HTML)
        match = re.search(r'src="data:image/webp;base64,([A-Za-z0-9+/=]+)"', LOGIN_HTML)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(
            base64.b64decode(match.group(1)),
            (ROOT / "web" / "bg-ruysch.webp").read_bytes(),
        )


@unittest.skipUnless(os.geteuid() == 0, "writing the page needs root")
class LoginPageInstallTests(unittest.TestCase):
    """The installer is idempotent, and it refuses a page without its markers."""

    def test_the_page_is_installed_once_and_converges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saved_source, saved_target = apply.LOGIN_SOURCE, apply.LOGIN_TARGET
            target = Path(tmp) / "login.html"
            setattr(apply, "LOGIN_SOURCE", LOGIN_PAGE)
            setattr(apply, "LOGIN_TARGET", target)
            try:
                first = io.StringIO()
                with contextlib.redirect_stdout(first):
                    changed = apply.ensure_login_page(False)
                second = io.StringIO()
                with contextlib.redirect_stdout(second):
                    again = apply.ensure_login_page(False)
            finally:
                setattr(apply, "LOGIN_SOURCE", saved_source)
                setattr(apply, "LOGIN_TARGET", saved_target)
            self.assertTrue(changed, first.getvalue())
            self.assertFalse(again, second.getvalue())
            self.assertIn("same", second.getvalue())
            self.assertEqual(target.read_bytes(), LOGIN_PAGE.read_bytes())

    def test_a_page_without_the_markers_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "login.html"
            source.write_text("<!doctype html><title>Hermes</title>", encoding="utf-8")
            saved_source = apply.LOGIN_SOURCE
            setattr(apply, "LOGIN_SOURCE", source)
            try:
                with self.assertRaises(SystemExit):
                    apply.ensure_login_page(True)
            finally:
                setattr(apply, "LOGIN_SOURCE", saved_source)
