"""ODL-505 on the demo host: the icon is first-party there too.

The tenant installer writes `/usr/local/lib/odile/icons`, serves the five site
paths and repoints the Webapp's three tab links. The demo machine was
provisioned before that cut, so `deploy/demo-host/deploy_demo_icons.py` is the
same write over the Webapp console. These tests pin what must travel before the
machine is touched, and what the machine must end up carrying:

* the five files the deploy uploads are the reviewed ones, byte for byte;
* the sign-in page links the three paths and carries no `data:` favicon;
* the three tab files the sign-in page links sit beside the page in
  `/srv/odile-demo` (t_6b25f6e5), so Caddy answers them itself, first-party
  and immutable, and the deploy publishes those three bytes under their pins;
* the Caddy block carries the three static routes plus the two page-server
  routes, all before the Webapp catch-all;
* the `/verify` template the deploy puts is the release page with the demo's
  one lede paragraph inside its tenant block, and the injection is idempotent.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy" / "demo-host"))

import apply_demo_icons  # noqa: E402
import apply_demo_public as apply  # noqa: E402
import check_demo_public as check  # noqa: E402
import deploy_demo_icons as deploy  # noqa: E402
import deploy_demo_public as deploy_public  # noqa: E402

DEMO = ROOT / "deploy" / "demo-host"
LOGIN_PAGE = DEMO / "login.html"
SITE_BLOCK = ROOT / "deploy" / "caddy" / "demo-tenant-site.caddy"
VERIFY_PAGE = ROOT / "web" / "verify.html"
VERIFY_LEDE = DEMO / "verify-lede.html"
VERIFY_COMMAND = DEMO / "verify-command.html"


class DemoIconFilesTests(unittest.TestCase):
    def test_the_five_uploaded_files_are_the_reviewed_ones(self) -> None:
        for name, pin, content_type in deploy.ICONS:
            data = (ROOT / "web" / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), pin, name)
            self.assertIn(content_type, ("image/x-icon", "image/png"))
        self.assertEqual(len(deploy.ICONS), 5)

    def test_the_plan_covers_the_icon_root_and_both_pages(self) -> None:
        remotes = {remote for _source, remote, _mode in deploy.PUTS}
        for name, _pin, _type in deploy.ICONS:
            self.assertIn(f"{deploy.ICON_ROOT}/{name}", remotes)
        for name in deploy.TAB_ICONS:
            self.assertIn(f"{deploy.DEMO_LIB}/{name}", remotes,
                          "the one-shot must put the page-root copies before it applies the block")
        self.assertIn(f"{deploy.LIB}/first_visit.py", remotes)
        self.assertIn(f"{deploy.LIB}/first-visit.html", remotes)
        self.assertIn(f"{deploy.LIB}/odile-webapp-brand.py", remotes)
        self.assertIn(f"{deploy.DEMO_LIB}/login.html", remotes)

    def test_the_local_checks_pass_on_this_tree(self) -> None:
        deploy.local_checks()


class DemoLoginIconTests(unittest.TestCase):
    def test_the_sign_in_page_links_the_three_paths(self) -> None:
        text = LOGIN_PAGE.read_text(encoding="utf-8")
        for line in deploy.LOGIN_LINKS:
            self.assertIn(line, text)
        self.assertNotIn("data:image/svg+xml", text)

    def test_the_block_serves_the_three_paths_from_the_page_root(self) -> None:
        # t_6b25f6e5: the three files the sign-in page links are beside the
        # page, so one exact handle answers them from Caddy's own file server.
        text = SITE_BLOCK.read_text(encoding="utf-8")
        catch_all = text.index("\n\thandle {\n")
        self.assertIn("@tabicons path /favicon.ico /icon-32.png /apple-touch-icon.png\n"
                      "\thandle @tabicons {", text)
        handle = ("\thandle @tabicons {\n"
                  "\t\troot * /srv/odile-demo\n"
                  '\t\theader Cache-Control "public, max-age=31536000, immutable"\n')
        self.assertIn(handle, text)
        self.assertLess(text.index(handle), catch_all)
        self.assertIn("\t\t@favicon path /favicon.ico\n"
                      "\t\theader @favicon Content-Type image/x-icon\n"
                      "\t\tfile_server", text)
        # The two sizes only the Webapp's shell links stay the page server's,
        # and the static handle must not carry a Cache-Control of its own.
        for name in ("icon-192.png", "icon-512.png"):
            route = f"handle /{name} {{\n\t\treverse_proxy 127.0.0.1:8091"
            self.assertIn(route, text)
            self.assertLess(text.index(route), catch_all)


class DemoTabPinsTests(unittest.TestCase):
    """The three bytes the sign-in page links are pinned at every hop."""

    def test_the_deploy_ships_the_committed_web_bytes_under_three_pins(self) -> None:
        self.assertEqual([name for name, _pin, _type in deploy_public.ICONS],
                         ["favicon.ico", "icon-32.png", "apple-touch-icon.png"])
        for name, pin, content_type in deploy_public.ICONS:
            data = (ROOT / "web" / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), pin, name)
            self.assertEqual(content_type,
                             "image/x-icon" if name.endswith(".ico") else "image/png")
            self.assertEqual(deploy_public.ICON_SOURCES[name], ROOT / "web" / name)
        self.assertEqual(tuple(deploy_public.ICONS), tuple(check.COMMITTED_ICONS))
        self.assertEqual(tuple(deploy_public.ICONS), tuple(apply.ICONS))

    def test_the_upload_plan_names_the_three_files_and_their_remote_paths(self) -> None:
        plan = deploy_public.icon_uploads("/tmp/odile-demo-public-dryrun")
        self.assertEqual([(source, remote) for source, remote, _mode in plan],
                         [(ROOT / "web" / name, f"/tmp/odile-demo-public-dryrun/{name}")
                          for name, _pin, _type in deploy_public.ICONS])
        self.assertEqual({mode for _source, _remote, mode in plan}, {"0644"})

    def test_the_apply_writes_them_beside_the_page_and_prints_same(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="odile-demo-tab-"))
        self.addCleanup(shutil.rmtree, tmp)
        self.addCleanup(setattr, apply, "SITE_ROOT", apply.SITE_ROOT)
        self.addCleanup(setattr, apply, "LIB", apply.LIB)
        apply.LIB = tmp
        apply.SITE_ROOT = tmp / "site"
        apply.SITE_ROOT.mkdir()
        for name, _pin, _type in apply.ICONS:
            shutil.copyfile(ROOT / "web" / name, tmp / name)
        self.assertTrue(apply.ensure_icons(dry=False))
        for name, pin, _type in apply.ICONS:
            target = apply.SITE_ROOT / name
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), pin)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        # A second run is `same`, not a rewrite.
        self.assertFalse(apply.ensure_icons(dry=False))
        # A source that is not the reviewed bytes is refused before any write.
        (tmp / "favicon.ico").write_bytes(b"not the reviewed icon")
        with self.assertRaises(SystemExit):
            apply.ensure_icons(dry=False)

    def test_the_check_reads_the_same_pins_and_the_page_head(self) -> None:
        page = LOGIN_PAGE.read_text(encoding="utf-8")
        self.assertIn(check.CREDIT_COMMENT, page)
        self.assertEqual(check.icon_problem(
            "icon-32.png", 200,
            {"Content-Type": "image/png",
             "Cache-Control": "public, max-age=31536000, immutable"},
            (ROOT / "web" / "icon-32.png").read_bytes()), "")
        self.assertNotEqual(check.icon_problem("favicon.ico", 404, {}, b""), "")
        self.assertNotEqual(check.icon_problem(
            "icon-32.png", 200,
            {"Content-Type": "image/png", "Cache-Control": "public, max-age=31536000, immutable"},
            b"not the icon"), "")
        self.assertNotEqual(check.icon_problem(
            "icon-32.png", 200,
            {"Content-Type": "text/html", "Cache-Control": "public, max-age=31536000, immutable"},
            (ROOT / "web" / "icon-32.png").read_bytes()), "")
        self.assertNotEqual(check.icon_problem(
            "apple-touch-icon.png", 200,
            {"Content-Type": "image/png", "Cache-Control": "no-store"},
            (ROOT / "web" / "apple-touch-icon.png").read_bytes()), "")
        self.assertEqual(check.icon_problem(
            "apple-touch-icon.png", 200,
            {"Content-Type": "image/png",
             "Cache-Control": "public, max-age=31536000, immutable"},
            (ROOT / "web" / "apple-touch-icon.png").read_bytes()), "")


class DemoVerifyTemplateTests(unittest.TestCase):
    def test_the_template_is_the_release_page_with_the_demo_lede(self) -> None:
        release = VERIFY_PAGE.read_text(encoding="utf-8")
        lede = VERIFY_LEDE.read_text(encoding="utf-8")
        wanted = apply.inject_lede(release, lede)
        for line in deploy.SITE_LINKS:
            self.assertIn(line, wanted)
        self.assertEqual(apply.inject_lede(wanted, lede), wanted)
        self.assertNotEqual(wanted, release)


class DemoIconsKeepsVerifyCommandTests(unittest.TestCase):
    """The icons deploy must not be the writer that drops the judge's command.

    ODL-563's root cause: ``deploy_demo_icons.py`` installs ``web/verify.html``
    as the release template and ``apply_demo_icons.py`` re-injected only the
    lede, so an icons deploy removed the ``<!--demo-check-->`` section the
    demo-public deploy had put there (journal 2026-11-15T01:09:17Z). The apply
    now converges both demo paragraphs, so either deploy leaves one pair.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-demo-icons-verify-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        for source in (VERIFY_LEDE, VERIFY_COMMAND):
            shutil.copyfile(source, self.tmp / source.name)
        self.page = self.tmp / "verify.html"
        # The apply module is shared with the demo-public tests; bind every
        # path it writes so the run cannot reach the live template.
        for name, value in (
            ("VERIFY_TEMPLATE", self.page),
            ("VERIFY_BACKUP", self.tmp / "verify.html.release"),
            ("__file__", str(self.tmp / "apply_demo_public.py")),
        ):
            self.addCleanup(setattr, apply, name, getattr(apply, name))
            setattr(apply, name, value)

    def converged(self) -> str:
        """The page the demo-public deploy leaves: demo lede plus one section."""
        return apply.inject_command(
            apply.inject_lede(VERIFY_PAGE.read_text(encoding="utf-8"),
                              VERIFY_LEDE.read_text(encoding="utf-8")),
            VERIFY_COMMAND.read_text(encoding="utf-8"))

    def assert_one_pair(self, text: str) -> None:
        self.assertEqual(text.count("<!--demo-check-->"), 1)
        self.assertEqual(text.count("<!--/demo-check-->"), 1)

    def test_the_icons_apply_keeps_the_pair_on_a_page_that_already_has_it(self):
        self.page.write_text(self.converged(), encoding="utf-8")
        before = self.page.read_text(encoding="utf-8")
        self.assertFalse(apply_demo_icons.converge_verify(dry=False))
        after = self.page.read_text(encoding="utf-8")
        self.assertEqual(after, before)
        self.assert_one_pair(after)

    def test_the_icons_apply_restores_the_pair_to_a_page_that_lost_it(self):
        # The state `deploy_demo_icons.py` leaves when it installs the release
        # page: the customer lede, no demo lede and no command section.
        self.page.write_text(VERIFY_PAGE.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertTrue(apply_demo_icons.converge_verify(dry=False))
        after = self.page.read_text(encoding="utf-8")
        self.assert_one_pair(after)
        self.assertIn(VERIFY_LEDE.read_text(encoding="utf-8").strip(), after)
        self.assertIn("./odile-verify https://demo.odile.fyi --expect {{MEASUREMENT}}", after)

    def test_the_deploy_uploads_the_sections_the_apply_reads_beside_it(self):
        remotes = {remote for _source, remote, _mode in deploy.PUTS}
        for name in ("verify-lede.html", "verify-command.html"):
            self.assertIn(f"{deploy.DEMO_LIB}/{name}", remotes,
                          "the apply must read this deploy's own section, not an "
                          "older installed copy")

    def test_the_deploy_compares_against_the_converged_page(self):
        # Comparing the raw release page made every run rewrite the template
        # (and drop the command until the apply re-added it). The deploy's
        # `same` target must be the page the apply leaves.
        self.assertEqual(deploy.template_bytes().decode("utf-8"), self.converged())


if __name__ == "__main__":
    unittest.main()
