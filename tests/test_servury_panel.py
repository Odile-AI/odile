"""The panel login is a form, so the parsing has to survive the form changing.

Servury's login is one 32-character credential plus a CSRF token — no email, no
OAuth. These tests pin the two things the tool reads from that page, so a panel
change fails loudly at `--login` instead of silently posting an empty form.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import servury_panel as panel  # noqa: E402

LOGIN_HTML = """
<main id="main">
  <form method="POST">
    <input type="hidden" name="csrf_token" value="e098eda55413ddeceff881a95132ec9c97a9a465c63c9daafdbb8c121c521d24">
    <label for="credential">Your Credential</label>
    <input type="password" id="credential" name="credential" maxlength="32" required autofocus>
    <input type="checkbox" name="remember_me">
    <button type="submit">Login</button>
  </form>
</main>
"""


class CsrfTests(unittest.TestCase):
    def test_the_token_is_read(self) -> None:
        self.assertEqual(
            panel.parse_csrf(LOGIN_HTML),
            "e098eda55413ddeceff881a95132ec9c97a9a465c63c9daafdbb8c121c521d24",
        )

    def test_a_page_without_the_token_returns_none(self) -> None:
        self.assertIsNone(panel.parse_csrf("<form><input name='credential'></form>"))

    def test_an_empty_body_is_not_a_crash(self) -> None:
        self.assertIsNone(panel.parse_csrf(""))


class InputTests(unittest.TestCase):
    def test_the_login_fields_are_named(self) -> None:
        self.assertEqual(panel.input_names(LOGIN_HTML), ["csrf_token", "credential", "remember_me"])

    def test_inputs_without_a_name_are_skipped(self) -> None:
        self.assertEqual(panel.input_names("<input type='hidden'><input name='x'>"), ["x"])


class CookieHeaderTests(unittest.TestCase):
    def test_the_pasted_header_is_split_into_pairs(self) -> None:
        # The panel rotates PHPSESSID; the jar needs the pairs, not the header.
        self.assertEqual(panel.header_pairs("PHPSESSID=abc; remember_token=def"),
                         [("PHPSESSID", "abc"), ("remember_token", "def")])

    def test_an_empty_header_is_no_pairs(self) -> None:
        self.assertEqual(panel.header_pairs(""), [])


class GuardTests(unittest.TestCase):
    """The credential can terminate this very machine, so the tool is fenced."""

    def setUp(self) -> None:
        # The live id is untracked (a fixture here), so the fence is tested
        # without depending on /root/.odile.
        self.id_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.id_dir.cleanup)
        self.id_file = Path(self.id_dir.name) / "servury_uki_server_id"
        self.id_file.write_text("a1b2c3d4\n", encoding="utf-8")
        patcher = mock.patch.object(panel, "SERVER_ID_FILE", self.id_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_destructive_path_is_refused(self) -> None:
        for path in ("/servers/x/delete", "/servers/x/terminate", "/api/reinstall", "/servers/x/power"):
            self.assertIsNotNone(panel.refuse_path(path), path)

    def test_a_boot_image_path_is_allowed(self) -> None:
        self.assertIsNone(panel.refuse_path("/servers/0badc0de/boot-image"))

    def test_only_the_test_box_is_allowed_by_default(self) -> None:
        self.assertTrue(panel.server_allowed("a1b2c3d4"))   # the fixture allow-list entry
        self.assertFalse(panel.server_allowed("e5f6a7b8"))   # the HQ fixture
        self.assertFalse(panel.server_allowed("0badc0de"))   # the demo tenant, other account
        self.assertTrue(panel.server_allowed("e5f6a7b8", frozenset({"e5f6a7b8"})))

    def test_a_missing_id_file_allows_nothing(self) -> None:
        # Fail closed: a fresh box has no /root/.odile file, so no default target.
        panel.SERVER_ID_FILE = Path(self.id_dir.name) / "absent"
        self.assertFalse(panel.server_allowed("a1b2c3d4"))
        self.assertTrue(panel.server_allowed("a1b2c3d4", frozenset({"a1b2c3d4"})))

    def test_an_unknown_form_field_is_refused(self) -> None:
        self.assertNotIn("delete_server", panel.POST_FIELDS)
        self.assertIn("disk_url", panel.POST_FIELDS)
        self.assertIn("disk_sha256", panel.POST_FIELDS)   # the published hash is sent with the image
        # The order form is not a write path any more: buying is the API's.
        self.assertNotIn("submit_order", panel.POST_FIELDS)
        self.assertNotIn("coupon", panel.POST_FIELDS)


SETTINGS_HTML = """
<main id="main"><h1>Settings</h1>
<dl class="bb-kv"><dt>User ID</dt><dd>lkbxu6i9</dd>
<dt>Balance</dt><dd>$20.00</dd></dl></main>
"""

# The dashboards the panel renders: one /manage/<id>/ link per machine the
# session's account owns, and nothing else is a machine of ours.
DASHBOARD_HTML = """
<main><h2>Your servers</h2>
<a href="/manage/168020b4/">odlukitest</a>
<a href="/manage/168020b4/renewal/">Renewal</a>
</main>
"""


class ConsoleTextTests(unittest.TestCase):
    def test_the_pre_block_is_the_transcript(self) -> None:
        html = '<section><h2>Serial output</h2><pre class="x">[ 1.0] bb-sealed-unlock: opened &amp; ok\nline2</pre></section>'

        text = panel.console_text(html)

        self.assertIn("bb-sealed-unlock: opened & ok", text)
        self.assertIn("line2", text)

    def test_a_page_with_the_log_off_is_empty(self) -> None:
        self.assertEqual(panel.console_text("<p>The console log is off, so there is nothing to show.</p>"), "")


class NoticeTests(unittest.TestCase):
    def test_stripe_javascript_is_not_a_refusal(self) -> None:
        html = '<script>throw new Error("Failed to create setup intent")</script><p class="bb-note">fine</p>'

        self.assertEqual(panel.refused_note(html), "")

    def test_a_notice_is_read_and_a_refusal_is_recognised(self) -> None:
        html = '<div class="bb-alert bb-err">url and sha256 are required</div>'

        self.assertIn("required", panel.refused_note(html))

    def test_the_real_flash_class_is_read(self) -> None:
        # The live page says <div class="bb-flash err">…</div>; a fixture with
        # only bb-alert let an "Invalid request" pass as success once.
        html = '<div class="bb-flash err">Invalid request. Reload the page and try again.</div>'

        self.assertIn("Invalid request", panel.refused_note(html))

    def test_the_power_and_boot_fields_are_allowed(self) -> None:
        for name in ("action", "enabled", "image_kernel_url", "image_kernel_sha256", "image_cmdline"):
            self.assertIn(name, panel.POST_FIELDS)


URL = "https://odile.fyi/releases/uki/" + "ab" * 8 + ".efi"
SHA = "ab" * 32
ATTACH_FORM = ('<form><input type="hidden" name="csrf_token" value="tok2">'
               '<input type="url" name="image_kernel_url"></form>')
ATTACHED = ('<input type="hidden" name="csrf_token" value="tok1">'
            '<dl class="bb-kv"><dt>Kernel</dt><dd><code>' + URL + '</code></dd>'
            '<dt>SHA-256</dt><dd><code>' + SHA + '</code></dd>'
            '<dt>Since</dt><dd>2026-11-12T19:08:32Z</dd></dl>')
DOWNLOADING = '<p class="bb-note">Fetching <code>' + URL + '</code> since 2026-11-12T21:38:43Z.</p>'

MANAGE_HTML = """
<form action="/manage/a1b2c3d4/admin/" method="post">
  <input type="hidden" name="csrf_token" value="tok">
  <input type="url" name="disk_url">
  <input type="text" name="disk_sha256">
  <select id="disk_format" name="disk_format">
    <option value="auto">auto</option>
  </select>
  <textarea name="image_cmdline"></textarea>
  <button type="submit">Save</button>
</form>
"""


class AuditIsolatedTests(unittest.TestCase):
    """A test write must land in a temp log, never the live panel log.

    `audit()` reads the module constant at call time, so swapping `panel.AUDIT`
    in setUp is enough to keep `/root/.odile/servury-panel.log` a record of real
    panel writes only. The live count is captured before the temp path is
    installed, so proof is a comparison rather than a promise.
    """

    def setUp(self) -> None:
        self.live_audit = panel.AUDIT
        self.audit_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.audit_dir.cleanup)
        self.audit_path = Path(self.audit_dir.name) / "servury-panel.log"
        panel.AUDIT = self.audit_path

    def tearDown(self) -> None:
        panel.AUDIT = self.live_audit

    def audit_lines(self) -> list[str]:
        try:
            return self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def live_count(self, marker: str) -> int:
        """Lines in the live log carrying `marker`; missing file is zero."""
        try:
            return sum(marker in line for line in self.live_audit.read_text(encoding="utf-8").splitlines())
        except OSError:
            return 0

    def stub_account(self, owned: tuple[str, ...] = ("a1b2c3d4",)) -> None:
        """Put the fixture machine on a named account, without a second page fetch.

        The account and its dashboard are the subject of `AccountTests`, which
        drives the real `owned_servers`/`account_of`; the older write tests only
        need the fence to pass, so they stub the two answers here.
        """
        for patcher in (
            mock.patch.object(panel, "_ACCOUNT", "lkbxu6i9"),
            mock.patch.object(panel, "owned_servers", lambda opener: frozenset(owned)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class _FakeJar:
    """The one call `login` makes on the jar: save the session it just won."""

    def __init__(self) -> None:
        self.saved = 0

    def save(self, **kwargs) -> None:  # noqa: ARG002 - the tool's flags
        self.saved += 1


# The dashboard the panel answers a successful login with. The logout control is
# what says the session is in; the login form carries none. (`dashboard` alone is
# not enough — the anonymous login page carries it in its meta keywords.)
PANEL_DASHBOARD = """
<main><h1>Your servers</h1>
<a href="/logout">Log out</a>
<a href="/manage/168020b4/">odlukitest</a>
</main>
"""


class LoginTests(AuditIsolatedTests):
    """`--login` is the one POST that carries the credential, and it is a form.

    The two field names are the login form's own (`credential`, `remember_me`),
    not a manage form's, so the fence accepts them only at the login path. The
    live form renders exactly `csrf_token`, `credential` and `remember_me`
    (`curl -sSL https://servury.com/login/`, measured 2026-11-20).
    """

    CREDENTIAL = "c" * 32

    def setUp(self) -> None:
        super().setUp()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.credential_file = Path(self.home.name) / "servury_panel_credential"
        self.credential_file.write_text(self.CREDENTIAL + "\n", encoding="utf-8")
        self.cookie_file = Path(self.home.name) / "servury_panel.cookies"
        self.cookie_file.write_text("", encoding="utf-8")
        for name, value in (("CREDENTIAL_FILE", self.credential_file),
                            ("COOKIE_FILE", self.cookie_file)):
            patcher = mock.patch.object(panel, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def wire_fields(opener) -> dict[str, list[str]]:
        """The urlencoded body `post()` actually sent."""
        return urllib.parse.parse_qs(opener.requests[0].data.decode("utf-8"))

    def run_login(self, get, post) -> tuple[int, str, str, _FakeJar]:
        out, err = io.StringIO(), io.StringIO()
        jar = _FakeJar()
        with mock.patch.object(panel, "get", get), \
                mock.patch.object(panel, "post", post), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = panel.login(None, jar)
        return code, out.getvalue(), err.getvalue(), jar

    def test_the_login_posts_the_two_fields_its_own_form_names(self) -> None:
        # The defect: `post()` refused both names before this card, so --login
        # answered "refusing unknown form field(s)" and never logged in.
        seen: dict = {}

        def fake_post(opener, url: str, fields: dict, **kwargs) -> str:  # noqa: ARG001
            seen.update(url=url, fields=dict(fields))
            return PANEL_DASHBOARD

        code, out, err, jar = self.run_login(lambda opener, url: LOGIN_HTML, fake_post)

        self.assertEqual(code, 0, err)
        self.assertEqual(seen["url"], panel.LOGIN)
        self.assertEqual(
            seen["fields"],
            {"csrf_token": panel.parse_csrf(LOGIN_HTML),
             "credential": self.CREDENTIAL, "remember_me": "on"},
        )
        self.assertEqual(jar.saved, 1)
        self.assertIn("dashboard", out, "the login has to say the dashboard answered")

    def test_the_fence_lets_the_login_fields_through_at_the_login_path(self) -> None:
        # The real `post()` (not a stub) is what the DoD's fence is: the two
        # names must reach the wire, urlencoded, from the login path.
        opener = _FakeOpener(PANEL_DASHBOARD)
        with mock.patch.object(panel, "_ACCOUNT", "lkbxu6i9"):
            text = panel.post(opener, panel.LOGIN, {
                "csrf_token": "tok", "credential": self.CREDENTIAL, "remember_me": "on",
            })

        self.assertEqual(text, PANEL_DASHBOARD)
        body = self.wire_fields(opener)
        self.assertEqual(body["credential"], [self.CREDENTIAL])
        self.assertEqual(body["remember_me"], ["on"])
        self.assertIn("POST " + panel.LOGIN, self.audit_lines()[0])

    def test_a_login_answers_the_dashboard_and_audits_no_account_yet(self) -> None:
        # The whole way through: the real `post()` fence, a fake panel that
        # answers the dashboard, and the audit line a login can honestly write.
        opener = _FakeOpener(PANEL_DASHBOARD)
        seen: list[str] = []

        def fake_get(opener, url: str) -> str:  # noqa: ARG001
            seen.append(url)
            return LOGIN_HTML

        with mock.patch.object(panel, "_ACCOUNT", None), \
                mock.patch.object(panel, "get", fake_get), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = panel.login(opener, _FakeJar())

        self.assertEqual(code, 0)
        self.assertEqual(seen, [panel.LOGIN], "no /settings/ fetch before there is a session")
        self.assertEqual(len(opener.requests), 1)
        body = self.wire_fields(opener)
        self.assertEqual(body["credential"], [self.CREDENTIAL])
        self.assertEqual(body["remember_me"], ["on"])
        line = self.audit_lines()[0]
        self.assertIn("POST " + panel.LOGIN, line)
        self.assertIn("account=?", line, "a login POST has no account yet; say so")
        self.assertIn("dashboard", out.getvalue())


    def test_the_login_fields_are_refused_away_from_the_login_path(self) -> None:
        # The narrow fix, not a widened allow-set: `credential` is a login name
        # only, so a manage form carrying it is still refused.
        with self.assertRaises(SystemExit) as caught:
            panel.post(_FakeOpener(), panel.BASE + "/manage/a1b2c3d4/admin/",
                       {"csrf_token": "t", "credential": self.CREDENTIAL})

        self.assertIn("unknown form field", str(caught.exception))
        self.assertIn("credential", str(caught.exception))

    def test_an_unknown_field_is_still_refused_at_an_allowed_path(self) -> None:
        # DoD: the fence keeps its meaning at the paths it already allowed.
        with self.assertRaises(SystemExit) as caught:
            panel.post(_FakeOpener(), panel.BASE + "/manage/a1b2c3d4/admin/",
                       {"csrf_token": "t", "username": "me"})

        self.assertIn("refusing unknown form field(s) ['username']", str(caught.exception))

    def test_an_unknown_field_is_refused_at_the_login_path_too(self) -> None:
        # The login set is a set, not a hole: a renamed login field still fails
        # loudly instead of being posted blind.
        with self.assertRaises(SystemExit) as caught:
            panel.post(_FakeOpener(), panel.LOGIN, {"csrf_token": "t", "username": "me"})

        self.assertIn("refusing unknown form field(s) ['username']", str(caught.exception))

    def test_a_form_rendered_again_is_a_refused_login(self) -> None:
        # The captcha gate answers the form again. That is a refusal, not a login.
        code, out, err, jar = self.run_login(lambda opener, url: LOGIN_HTML,
                                             lambda opener, url, fields, **kw: LOGIN_HTML)

        self.assertEqual(code, 1)
        self.assertIn("dashboard", err)
        self.assertEqual(jar.saved, 0, "a refused login must not save the jar")
        self.assertNotIn("logged in", out)

    def test_an_existing_session_is_recognised_before_any_post(self) -> None:
        def fake_get(opener, url: str) -> str:  # noqa: ARG001
            return SETTINGS_HTML if url.rstrip("/").endswith("settings") else PANEL_DASHBOARD

        posted: list[tuple] = []
        code, out, err, _ = self.run_login(
            fake_get, lambda *args, **kwargs: posted.append(args) or "<p>ok</p>")

        self.assertEqual(code, 0, err)
        self.assertEqual(posted, [], "a valid session redirects /login/; nothing to post")
        self.assertIn("already logged in", out)
        self.assertIn("user lkbxu6i9", out)


class OrderRouteTests(AuditIsolatedTests):
    """Buying is an API call at the partner price, not a panel form post.

    The panel order form charges list price even on an account holding a key, and
    the affiliate route that used to sit in front of it is retired (2026-11-19).
    `?action=create` with the account's key is 5% off orders, renewals and
    upgrades, so ordering moved there and the panel can no longer post an order.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(panel, "_ACCOUNT", "lkbxu6i9")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = Path(self.audit_dir.name) / "servury_reseller_key_lkbxu6i9"
        self.key.write_text("k" * 64 + "\n", encoding="utf-8")
        self.pub = Path(self.audit_dir.name) / "id_ed25519.pub"
        self.pub.write_text("ssh-ed25519 AAAA test@host\n", encoding="utf-8")

    def test_the_order_posts_to_the_api_with_the_key_and_the_ssh_key(self) -> None:
        seen: dict = {}

        def fake_api(key, plan, os_name, days, hostname, ssh_key, preorder=False):
            seen.update(key=key, plan=plan, os=os_name, days=days, hostname=hostname, ssh_key=ssh_key)
            return {"server_id": "abc12345", "hostname": hostname, "status": "deploying",
                    "amount_charged": 9.49}

        with mock.patch.object(panel, "api_order", fake_api):
            code = panel.order(None, "108", "2", "debian13", "31", "odl600", True,
                               str(self.pub), str(self.key))

        self.assertEqual(code, 0)
        self.assertEqual(seen["key"], "k" * 64)
        self.assertEqual(seen["plan"], "108")
        self.assertEqual(seen["ssh_key"], "ssh-ed25519 AAAA test@host")
        self.assertEqual(seen["days"], "31")

    def test_the_order_line_names_the_api_route_and_the_key(self) -> None:
        with mock.patch.object(panel, "api_order",
                               return_value={"server_id": "abc12345", "hostname": "odl600",
                                             "status": "deploying", "amount_charged": "9.49"}):
            panel.order(None, "108", "2", "debian13", "31", "odl600", True,
                        str(self.pub), str(self.key))

        line = self.audit_lines()[0]
        self.assertIn("ORDER route=api plan=108", line)
        self.assertIn("key=", line)
        self.assertIn("account=lkbxu6i9", line)
        self.assertNotIn("coupon", line)

    def test_an_order_without_an_ssh_key_is_refused(self) -> None:
        with mock.patch.object(panel, "api_order") as api:
            code = panel.order(None, "108", "2", "debian13", "31", "odl600", True, "", str(self.key))
        self.assertEqual(code, 2)
        api.assert_not_called()

    def test_the_panel_order_form_is_not_a_post_path(self) -> None:
        # The one fence that makes a list-price order impossible through the tool.
        with self.assertRaises(SystemExit):
            panel.post(None, panel.BASE + "/order/?plan=108", {"plan": "108"})


class AccountTests(AuditIsolatedTests):
    """Every write names the account it landed in, and only that account's boxes.

    The panel credential is one login for a whole account. Once the tenant lives
    on the account the team owns, a line that says `server=<id>` no longer says
    enough: two accounts hold machines, and the log has to answer "which
    account did this land in" without the reader running `--whoami` afterwards.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(panel, "_ACCOUNT", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pages(self, dashboard: str = DASHBOARD_HTML, settings: str = SETTINGS_HTML):
        """A fake `get` that answers the dashboard and settings pages, and counts."""
        seen: list[str] = []

        def fake_get(opener, url: str) -> str:
            seen.append(url)
            if url.rstrip("/").endswith("settings"):
                return settings
            if url.rstrip("/").endswith("dashboard"):
                return dashboard
            return ATTACH_FORM

        return fake_get, seen

    def test_the_owned_set_is_the_dashboard_links(self) -> None:
        fake_get, _ = self.pages()
        with mock.patch.object(panel, "get", fake_get):
            self.assertEqual(panel.owned_servers(None), frozenset({"168020b4"}))

    def test_the_account_is_read_once_per_run(self) -> None:
        fake_get, seen = self.pages()
        with mock.patch.object(panel, "get", fake_get):
            self.assertEqual(panel.account_of(None), "lkbxu6i9")
            self.assertEqual(panel.account_of(None), "lkbxu6i9")

        self.assertEqual(sum(url.rstrip("/").endswith("settings") for url in seen), 1)

    def test_a_boot_write_names_the_account_in_its_line(self) -> None:
        fake_get, _ = self.pages()
        with mock.patch.object(panel, "get", fake_get), \
                mock.patch.object(panel, "post", return_value=ATTACHED):
            code = panel.write_boot_image(None, "168020b4", URL, SHA, "", apply=True,
                                          wait=0, interval=0)

        self.assertEqual(code, 0)
        lines = self.audit_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("account=lkbxu6i9", lines[0])
        self.assertIn("BOOT_IMAGE server=168020b4", lines[0])

    def test_a_disk_write_names_the_account_in_its_line(self) -> None:
        fake_get, _ = self.pages()
        with mock.patch.object(panel, "get", fake_get), \
                mock.patch.object(panel, "post", return_value="<p>queued</p>"):
            code = panel.write_disk(None, "168020b4", "https://odile.fyi/x.raw", "raw",
                                    apply=True, sha256=SHA)

        self.assertEqual(code, 0)
        lines = self.audit_lines()
        self.assertTrue(lines)
        self.assertIn("account=lkbxu6i9", lines[0])
        self.assertIn("DISK_WRITE server=168020b4", lines[0])

    def test_a_machine_from_another_account_is_refused(self) -> None:
        # 31a63bbb is the demo tenant on the reseller account: another login owns
        # it, so it is not in this session's dashboard and no form may name it.
        fake_get, _ = self.pages()
        posted: list[tuple] = []
        with mock.patch.object(panel, "get", fake_get), \
                mock.patch.object(panel, "post", side_effect=lambda *a: posted.append(a) or ""):
            boot = panel.write_boot_image(None, "31a63bbb", URL, SHA, "", apply=True,
                                          wait=0, interval=0)
            disk = panel.write_disk(None, "31a63bbb", "https://odile.fyi/x.raw", "raw",
                                    apply=True, sha256=SHA)

        self.assertEqual((boot, disk), (2, 2))
        self.assertEqual(posted, [], "a foreign machine must not be named in a POST")
        self.assertFalse(any("31a63bbb" in line for line in self.audit_lines()))

    def test_a_dry_run_on_a_foreign_machine_is_refused_too(self) -> None:
        fake_get, _ = self.pages()
        with mock.patch.object(panel, "get", fake_get):
            code = panel.write_disk(None, "31a63bbb", "https://odile.fyi/x.raw", "raw",
                                    apply=False)

        self.assertEqual(code, 2)

    def test_an_unreadable_account_refuses_the_write(self) -> None:
        # A settings page that does not name the id (a panel change, or a session
        # that is not really logged in) must not produce a write with no account.
        fake_get, _ = self.pages(settings="<main><h1>Settings</h1></main>")
        with mock.patch.object(panel, "get", fake_get), \
                mock.patch.object(panel, "post", return_value="<p>queued</p>"):
            code = panel.write_disk(None, "168020b4", "https://odile.fyi/x.raw", "raw",
                                    apply=True, sha256=SHA)

        self.assertEqual(code, 2)

    def test_power_and_console_name_the_account(self) -> None:
        fake_get, _ = self.pages()

        def fake_get_console(opener, url: str) -> str:
            if "/console/" in url:
                return '<input name="csrf_token" value="t"><p>Console log: on</p>'
            return fake_get(opener, url)

        with mock.patch.object(panel, "get", fake_get_console), \
                mock.patch.object(panel, "post", return_value="<p>Console log is on.</p>"):
            power = panel.power(None, "168020b4", "restart")
            console = panel.console(None, "168020b4", "on")

        self.assertEqual((power, console), (0, 0))
        lines = self.audit_lines()
        self.assertEqual(len(lines), 2, lines)
        for line in lines:
            self.assertIn("account=lkbxu6i9", line)


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body


class _FakeOpener:
    """The one call `post` makes: `open(request, timeout=…)` as a context."""

    def __init__(self, body: str = "<p>ok</p>") -> None:
        self.requests: list[urllib.request.Request] = []
        self.body = body

    def open(self, request, timeout=None):  # noqa: ARG002 - the tool's timeout
        self.requests.append(request)
        return contextlib.nullcontext(_FakeResponse(self.body))


class PostLineTests(AuditIsolatedTests):
    """The POST line itself names the account, so no reader has to infer it.

    The DoD is read from the log alone, and the write line is not the only line
    a reader sees: each write is followed by a `POST` line. Reading the account
    through `account_of` (not the module global) means a write path that ever
    posts before its audit line still leaves a line naming the account.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(panel, "_ACCOUNT", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_post_line_names_the_account_without_a_prior_audit_line(self) -> None:
        opener = _FakeOpener()
        with mock.patch.object(panel, "get", return_value=SETTINGS_HTML):
            text = panel.post(opener, panel.BASE + "/manage/168020b4/admin/",
                              {"action": "restart", "csrf_token": "t"})

        self.assertEqual(text, "<p>ok</p>")
        lines = self.audit_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("POST " + panel.BASE + "/manage/168020b4/admin/", lines[0])
        self.assertIn("account=lkbxu6i9", lines[0])
        self.assertEqual(len(opener.requests), 1, "the form was posted once")

    def test_an_unreadable_account_is_named_unknown_never_omitted(self) -> None:
        # The line must still carry the field, so a reader cannot mistake a
        # missing account for a write made by an account they do not know of.
        with mock.patch.object(panel, "get", return_value="<main><h1>Settings</h1></main>"):
            panel.post(_FakeOpener(), panel.BASE + "/manage/168020b4/admin/",
                       {"action": "restart"})

        line = self.audit_lines()[0]
        self.assertIn("account=?", line)


class BootImageWriteTests(AuditIsolatedTests):
    """Replacing the boot image needs detach → attach → wait for ready."""

    def setUp(self) -> None:
        super().setUp()
        self.stub_account()

    def run_write(self, pages: list[str], results: list[str], **kwargs):
        calls: list[dict] = []

        def fake_get(opener, url: str) -> str:
            return pages.pop(0)

        def fake_post(opener, url: str, fields: dict) -> str:
            calls.append(fields)
            return results.pop(0)

        with mock.patch.object(panel, "get", fake_get), mock.patch.object(panel, "post", fake_post):
            code = panel.write_boot_image(
                None, "a1b2c3d4", URL, SHA, "", apply=True,
                wait=kwargs.pop("wait", 1), interval=0, **kwargs)
        return code, calls

    def test_a_machine_that_already_boots_a_kernel_is_detached_first(self) -> None:
        code, calls = self.run_write([ATTACHED], [ATTACH_FORM, ATTACHED])

        self.assertEqual(code, 0)
        self.assertEqual([call["action"] for call in calls], ["image_detach", "image_attach"])
        self.assertEqual(calls[1]["image_kernel_url"], URL)
        self.assertEqual(calls[1]["image_kernel_sha256"], SHA)
        self.assertEqual(calls[1]["image_cmdline"], "")
        self.assertEqual(calls[1]["csrf_token"], "tok2", "the detach rotates the token")

    def test_a_machine_with_no_image_attaches_without_a_detach(self) -> None:
        code, calls = self.run_write([ATTACH_FORM], [ATTACHED])

        self.assertEqual(code, 0)
        self.assertEqual([call["action"] for call in calls], ["image_attach"])

    def test_a_download_that_never_becomes_ready_fails(self) -> None:
        code, _ = self.run_write([ATTACH_FORM, DOWNLOADING], [DOWNLOADING], wait=0)

        self.assertEqual(code, 1)

    def test_a_refusal_from_the_panel_fails(self) -> None:
        refused = '<div class="bb-flash err">Invalid request. Reload the page and try again.</div>'

        code, _ = self.run_write([ATTACH_FORM], [refused])

        self.assertEqual(code, 1)

    def test_a_dry_run_posts_nothing(self) -> None:
        calls: list[tuple] = []
        with mock.patch.object(panel, "get", return_value=ATTACHED), \
                mock.patch.object(panel, "post", side_effect=lambda *a: calls.append(a)):
            code = panel.write_boot_image(None, "a1b2c3d4", URL, SHA, "", apply=False)

        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_a_write_is_audited_to_the_temp_log_not_the_live_log(self) -> None:
        live_before = self.live_count("abababab")
        code, _ = self.run_write([ATTACH_FORM], [ATTACHED])

        self.assertEqual(code, 0)
        lines = self.audit_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("BOOT_IMAGE server=a1b2c3d4", lines[0])
        self.assertIn("abababab", lines[0])
        self.assertEqual(self.live_count("abababab"), live_before,
                         "make check must not append fake writes to the live panel log")

    def test_the_since_row_is_what_says_the_image_is_in_place(self) -> None:
        self.assertTrue(panel.attached_image(ATTACHED))
        self.assertFalse(panel.attached_image(DOWNLOADING))


class DumpTests(unittest.TestCase):
    """`--dump` is how the field names are confirmed, so it must see every control."""

    def dump(self, html: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(panel, "get", return_value=html), contextlib.redirect_stdout(out):
            code = panel.dump(None, "a1b2c3d4")
        return code, out.getvalue()

    def test_a_select_field_is_named_in_the_dump(self) -> None:
        code, out = self.dump(MANAGE_HTML)

        self.assertEqual(code, 0)
        self.assertIn("disk_format", out, "the manage form's format is a <select>, not an <input>")
        self.assertIn("disk_url", out)
        self.assertIn("disk_sha256", out)

    def test_a_textarea_field_is_named_in_the_dump(self) -> None:
        code, out = self.dump(MANAGE_HTML)

        self.assertEqual(code, 0)
        self.assertIn("image_cmdline", out)

    def test_an_input_only_page_still_lists_its_inputs(self) -> None:
        code, out = self.dump(LOGIN_HTML)

        self.assertEqual(code, 0)
        for name in ("csrf_token", "credential", "remember_me"):
            self.assertIn(name, out)

    def test_a_page_with_no_form_fields_still_fails(self) -> None:
        code, _ = self.dump("<p>no form here</p>")

        self.assertEqual(code, 1)


class SessionTests(unittest.TestCase):
    def test_the_pasted_header_seeds_a_clean_jar(self) -> None:
        with mock.patch.object(panel, "header_pairs", return_value=[("PHPSESSID", "abc")]):
            _, jar = panel.opener_for(Path("/nonexistent/cookies"))

        self.assertEqual([(c.name, c.value) for c in jar], [("PHPSESSID", "abc")])
        self.assertIsNone(panel._JAR, "the paste is the source of truth; nothing to persist")


class PowerTests(AuditIsolatedTests):
    def setUp(self) -> None:
        super().setUp()
        self.stub_account()

    def test_a_504_is_sent_not_failed(self) -> None:
        # Measured: the gateway gives up at 30 s, the node reboots anyway.
        error = urllib.error.HTTPError("https://servury.com", 504, "Gateway Timeout", None, None)
        with mock.patch.object(panel, "get", return_value='<input name="csrf_token" value="t">'), \
                mock.patch.object(panel, "post", side_effect=error):
            code = panel.power(None, "a1b2c3d4", "restart")

        self.assertEqual(code, 0)

    def test_a_power_action_is_audited_to_the_temp_log_not_the_live_log(self) -> None:
        live_before = self.live_count("POWER server=a1b2c3d4")
        with mock.patch.object(panel, "get", return_value='<input name="csrf_token" value="t">'), \
                mock.patch.object(panel, "post", return_value="<p>ok</p>"):
            code = panel.power(None, "a1b2c3d4", "restart")

        self.assertEqual(code, 0)
        self.assertTrue(any("POWER server=a1b2c3d4 action=restart" in line
                            for line in self.audit_lines()))
        self.assertEqual(self.live_count("POWER server=a1b2c3d4"), live_before)


class OrderDefaultsTests(AuditIsolatedTests):
    """The shipped order default is a Nano at the annual term, never a Core.

    Savio's 2026-11-24 direction makes the new machine a Blackbox Nano (plan
    138, the Canada 1 vCPU / 2 GB / 25 GB tier) at 365 days. The tier matters
    because Servury cannot downgrade a machine: a tester placed on a Core cannot
    move to a Nano without a rebuild.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(panel, "_ACCOUNT", "lkbxu6i9")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = Path(self.audit_dir.name) / "servury_reseller_key_lkbxu6i9"
        self.key.write_text("k" * 64 + "\n", encoding="utf-8")
        self.pub = Path(self.audit_dir.name) / "id_ed25519.pub"
        self.pub.write_text("ssh-ed25519 AAAA test@host\n", encoding="utf-8")

    def test_an_order_with_no_flags_is_a_nano_for_a_year(self) -> None:
        args = panel.build_parser().parse_args(["--order"])
        self.assertEqual(args.plan, "138")
        self.assertEqual(args.days, "365")

    def test_an_explicit_plan_and_term_still_win(self) -> None:
        args = panel.build_parser().parse_args(
            ["--order", "--plan", "113", "--days", "31"])
        self.assertEqual((args.plan, args.days), ("113", "31"))

    def test_the_dry_run_of_the_defaults_names_the_nano_and_the_year(self) -> None:
        args = panel.build_parser().parse_args([
            "--order", "--hostname", "odl600",
            "--ssh-key-file", str(self.pub), "--key-file", str(self.key),
        ])
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = panel.order(
                None, args.plan, args.location, args.os_name or "debian12", args.days,
                args.hostname, args.apply, args.ssh_key_file, args.key_file, args.preorder,
            )
        self.assertEqual(code, 0)
        self.assertIn("would order plan 138 for 365 days", printed.getvalue())
        self.assertIn("ORDER route=api plan=138", self.audit_lines()[0])


if __name__ == "__main__":
    unittest.main()
