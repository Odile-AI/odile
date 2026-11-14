"""ODL-505 on the demo host: the icon is first-party there too.

The tenant installer writes `/usr/local/lib/odile/icons`, serves the five site
paths and repoints the Webapp's three tab links. The demo machine was
provisioned before that cut, so `deploy/demo-host/deploy_demo_icons.py` is the
same write over the Webapp console. These tests pin what must travel before the
machine is touched, and what the machine must end up carrying:

* the five files the deploy uploads are the reviewed ones, byte for byte;
* the sign-in page links the three paths and carries no `data:` favicon;
* the Caddy block carries the five exact routes, before the Webapp catch-all;
* the `/verify` template the deploy puts is the release page with the demo's
  one lede paragraph inside its tenant block, and the injection is idempotent.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy" / "demo-host"))

import apply_demo_public as apply  # noqa: E402
import deploy_demo_icons as deploy  # noqa: E402

LOGIN_PAGE = ROOT / "deploy" / "demo-host" / "login.html"
SITE_BLOCK = ROOT / "deploy" / "caddy" / "demo-tenant-site.caddy"
VERIFY_PAGE = ROOT / "web" / "verify.html"
VERIFY_LEDE = ROOT / "deploy" / "demo-host" / "verify-lede.html"


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

    def test_the_block_has_the_five_exact_routes_before_the_catch_all(self) -> None:
        text = SITE_BLOCK.read_text(encoding="utf-8")
        catch_all = text.index("\n\thandle {\n")
        for name, _pin, _type in deploy.ICONS:
            route = f"handle /{name} {{\n\t\treverse_proxy 127.0.0.1:8091"
            self.assertIn(route, text)
            self.assertLess(text.index(route), catch_all)
            self.assertNotIn(f"handle /{name} {{\n\t\theader", text)


class DemoVerifyTemplateTests(unittest.TestCase):
    def test_the_template_is_the_release_page_with_the_demo_lede(self) -> None:
        release = VERIFY_PAGE.read_text(encoding="utf-8")
        lede = VERIFY_LEDE.read_text(encoding="utf-8")
        wanted = apply.inject_lede(release, lede)
        for line in deploy.SITE_LINKS:
            self.assertIn(line, wanted)
        self.assertEqual(apply.inject_lede(wanted, lede), wanted)
        self.assertNotEqual(wanted, release)


if __name__ == "__main__":
    unittest.main()
