"""The Danger Zone write is the one that erases a disk, so it is fenced twice.

The reinstall form does not live under a path that names a forbidden verb —
it posts to `/manage/<id>/danger/`, and the destroy form sits in the same block
one word away (`action=delete`, the literal `DELETE`). The existing path fence
therefore cannot see it, and these tests pin the second fence: the **field**
`action=reinstall` is permitted only by the verb that carries it, and
`action=delete` is never permitted at all.

Everything else the tool must not "simplify" is pinned here too, over the form
recorded on 2026-11-19 (`spike/servury-reinstall/evidence/danger-dump.txt` and
`danger-page.txt`): `ERASE` is a constant typed by the tool and not an argument,
the page must still carry "Erase and reinstall", the four build-time fields have
no defaults, the SSH key comes from a file, and the audit line is written
*before* the POST with the boot/disk pair the write is about to lose.

The audit log is a live host file, so every test swaps `panel.AUDIT` for a temp
path and (where it matters) proves the live file did not move.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import servury_panel as panel  # noqa: E402

SERVER = "168020b4"
# The tree's public stand-in for a second machine: `0badc0de` is on the leak
# scanner's instance-id allow-list and `odl-fake01` is the fake name the leak
# tests use, so this fixture never publishes a live tenant's identity.
OTHER = "0badc0de"
KEY_TEXT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILYSlv4CODJQwZPGJc6RHk/OpldJkUHacfUHwj9rmmev odile-uki-test"
BOOT_URL = "https://odile.fyi/releases/uki/f7bc0ceba60442b0.efi"
BOOT_SHA = "f7bc0ceba60442b0" + "0" * 48
DISK_URL = "https://odile.fyi/releases/disks/61882ae1d2748539.raw"
DISK_SHA = "61882ae1d2748539" + "1" * 48

# The recorded Danger Zone page, trimmed to the two forms and the header row.
# Every control name, its default and the two button labels are the ones the
# page served on 2026-11-19 (42,323 bytes; the dump lists `action` twice, once
# per form). The destroy form is kept: it is what the field fence must exclude.
DANGER_HTML = """
<header><b>odlukitest</b> <span>168020b4</span> <span>Running</span></header>
<h2>Reinstall</h2>
<p>Rebuilds the machine from a fresh image. Everything on the disk is destroyed.</p>
<details><summary>Reinstall this server</summary><div class="in">
  <form method="post">
    <input type="hidden" name="csrf_token" value="tok-danger">
    <label for="os">Operating system</label>
    <select id="os" name="os">
      <option value="debian13" selected>Debian 13</option>
      <option value="debian12">Debian 12</option>
    </select>
    <fieldset><legend>Machine configuration</legend>
      <input type="checkbox" name="want_snp" value="1" checked>
      <input type="checkbox" name="want_fde" value="1" checked>
      <input type="checkbox" name="want_measured" value="1" checked>
    </fieldset>
    <fieldset><legend>Credentials for the fresh install</legend>
      <input type="password" id="np" name="new_password" minlength="8">
      <input type="password" id="npc" name="new_password_confirm">
      <textarea id="rk" name="ssh_key" placeholder="ssh-ed25519 AAAA...">""" + KEY_TEXT + """</textarea>
      <small class="d">Whatever is in this box becomes the whole authorized_keys file.</small>
    </fieldset>
    <label for="cf">Type ERASE to confirm</label>
    <input type="text" id="cf" name="confirm" pattern="ERASE" placeholder="ERASE" required>
    <button class="bb-btn wide" name="action" value="reinstall">Erase and reinstall</button>
  </form>
</div></details>
<section class="bb-sec">
<h2>Destroy this server</h2>
<form method="post">
  <input type="hidden" name="csrf_token" value="tok-destroy">
  <input type="text" name="confirm" placeholder="DELETE" required>
  <button class="bb-btn" name="action" value="delete">Destroy permanently</button>
</form>
</section>
"""

# The same page after the vendor changed it: the control is gone, so the tool
# must refuse rather than post ERASE at a form it no longer recognises.
DANGER_NO_CONTROL = DANGER_HTML.replace("Erase and reinstall", "Wipe this machine")

ADMIN_HTML = """
<section class="bb-sec">
  <h2>Boot image<span class="r">Your image</span></h2>
  <dl class="bb-kv">
    <dt>Kernel</dt><dd><code>""" + BOOT_URL + """</code></dd>
    <dt>SHA-256</dt><dd><code>""" + BOOT_SHA + """</code></dd>
    <dt>Command line</dt><dd><span class="text-muted">empty: the command line inside your UKI applies</span></dd>
    <dt>Since</dt><dd>2026-11-19T13:16:52Z</dd>
  </dl>
</section>
<section class="bb-sec">
  <h2>Disk image<span class="r">Your image</span></h2>
  <dl class="bb-kv">
    <dt>Written</dt><dd>2026-11-19T13:18:08Z</dd>
    <dt>Image</dt><dd><code>""" + DISK_URL + """</code> <span>(raw, 8.0 GiB virtual)</span></dd>
    <dt>SHA-256</dt><dd><code>""" + DISK_SHA + """</code></dd>
  </dl>
</section>
"""

# What the same page reads after a reinstall: both image fields are cleared.
ADMIN_EMPTY = """
<section class="bb-sec">
  <h2>Boot image</h2>
  <p class="bb-note">By default that is our published release.</p>
</section>
<section class="bb-sec">
  <h2>Disk image</h2>
  <p class="bb-note">Write your own disk image</p>
</section>
"""

SETTINGS_HTML = """
<main id="settings"><h1>Settings</h1>
<dl class="bb-kv"><dt>User ID</dt><dd>lkbxu6i9</dd><dt>Balance</dt><dd>$7.86</dd></dl></main>
"""
DASHBOARD_HTML = """
<main><h2>Your servers</h2>
<a href="/manage/168020b4/">odlukitest</a>
<a href="/manage/0badc0de/">odl-fake01</a>
</main>
"""

# The panel page `console()` reads before it arms and after it POSTs. The token
# is the point: with it in the fixture, a `--recycle` that skips the one-id
# fence actually POSTs the console write on the pre-fix tree, so the tests'
# `posts == []` is what fails — not an unrelated "no csrf_token" exit 1.
CONSOLE_HTML = """
<header><span>Running</span> <span>Console log off</span></header>
<form method="post">
  <input type="hidden" name="csrf_token" value="tok-console">
  <label><input type="checkbox" name="enabled"> Console log</label>
  <button>Save</button>
</form>
"""


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body


class _FakeOpener:
    """The one call `post` makes: `open(request, timeout=…)` as a context."""

    def __init__(self, body: str = "<p>ok</p>") -> None:
        self.requests: list[object] = []
        self.body = body

    def open(self, request, timeout=None):  # noqa: ARG002 - the tool's timeout
        self.requests.append(request)
        return contextlib.nullcontext(_FakeResponse(self.body))


class FencedCase(unittest.TestCase):
    """A temp audit log and a one-id allow-list, so no test touches /root/.odile."""

    def setUp(self) -> None:
        self.live_audit = panel.AUDIT
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.audit_path = Path(self.dir.name) / "servury-panel.log"
        panel.AUDIT = self.audit_path
        self.addCleanup(setattr, panel, "AUDIT", self.live_audit)

        self.id_file = Path(self.dir.name) / "servury_uki_server_id"
        self.id_file.write_text(SERVER + "\n", encoding="utf-8")
        patcher = mock.patch.object(panel, "SERVER_ID_FILE", self.id_file)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.key_file = Path(self.dir.name) / "id_ed25519.pub"
        self.key_file.write_text(KEY_TEXT + "\n", encoding="utf-8")

        # The account and its dashboard are exercised in test_servury_panel; the
        # write tests only need the fence to pass without a second page fetch.
        for name, value in (("_ACCOUNT", "lkbxu6i9"),
                            ("owned_servers", lambda opener: frozenset({SERVER}))):
            patcher = mock.patch.object(panel, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def lines(self) -> list[str]:
        try:
            return self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def live_lines(self) -> list[str]:
        try:
            return self.live_audit.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def argv(self, *extra: str) -> list[str]:
        return [
            "--reinstall", "--server", SERVER, "--os", "debian13",
            "--want-snp", "--want-fde", "--want-measured",
            "--ssh-key-file", str(self.key_file), *extra,
        ]

    def run_tool(self, argv: list[str], *, danger: str = DANGER_HTML,
                 admin: str = ADMIN_HTML, manage: str = CONSOLE_HTML,
                 posts: list | None = None, bodies: list[str] | None = None
                 ) -> tuple[int, str, str]:
        """Drive main() with the panel faked; return (exit, stdout, stderr)."""
        posts = posts if posts is not None else []
        queue = list(bodies or [])

        def fake_get(opener, url: str) -> str:
            if url.rstrip("/").endswith("settings"):
                return SETTINGS_HTML
            if url.rstrip("/").endswith("dashboard"):
                return DASHBOARD_HTML
            if "/danger/" in url:
                return danger
            if "/admin/" in url:
                return admin
            if "/manage/" in url:
                return manage
            return admin

        def fake_post(opener, url: str, fields: dict, **kwargs) -> str:
            posts.append({"url": url, "fields": dict(fields), "kwargs": kwargs,
                          "audit_lines_before": len(self.lines())})
            if queue:
                return queue.pop(0)
            return '<div class="bb-flash ok">Reinstall queued. The machine is rebuilt in the background.</div>'

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(panel, "get", fake_get), \
                mock.patch.object(panel, "post", fake_post), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = panel.main(argv)
        return code, out.getvalue(), err.getvalue()


class RecordedFormTests(unittest.TestCase):
    """The fixture is the recorded form: field names, defaults, both buttons."""

    def test_every_field_the_post_sends_is_on_the_recorded_form(self) -> None:
        names = [name for _kind, name in panel.field_names(DANGER_HTML)]
        for name in ("csrf_token", "os", "want_snp", "want_fde", "want_measured",
                     "new_password", "new_password_confirm", "ssh_key", "confirm", "action"):
            self.assertIn(name, names)

    def test_the_control_the_tool_requires_is_on_the_page(self) -> None:
        self.assertIn("Erase and reinstall", DANGER_HTML)

    def test_the_destroy_form_is_one_word_away(self) -> None:
        # The reason the path guard alone is not the fence.
        self.assertIn('name="action" value="delete"', DANGER_HTML)
        self.assertIn('name="action" value="reinstall"', DANGER_HTML)
        self.assertIsNone(panel.refuse_path("/manage/168020b4/danger/"))


class FieldFenceTests(FencedCase):
    """`action` is the fence: reinstall only by its verb, delete never."""

    def test_the_reinstall_field_is_refused_at_any_other_path(self) -> None:
        opener = _FakeOpener()
        with self.assertRaises(SystemExit) as raised:
            panel.post(opener, panel.BASE + "/manage/168020b4/admin/",
                       {"action": "reinstall", "csrf_token": "t"})
        self.assertIn("action=reinstall", str(raised.exception))
        self.assertEqual(opener.requests, [], "a refused field must not be posted")

    def test_the_verb_that_owns_the_field_can_post_it(self) -> None:
        opener = _FakeOpener()
        text = panel.post(opener, panel.BASE + "/manage/168020b4/danger/",
                          {"action": "reinstall", "csrf_token": "t"},
                          permitted_actions=("reinstall",))
        self.assertEqual(text, "<p>ok</p>")
        self.assertEqual(len(opener.requests), 1)

    def test_delete_is_never_permitted(self) -> None:
        for permitted in ((), ("reinstall",), ("delete",)):
            with self.assertRaises(SystemExit) as raised:
                panel.post(_FakeOpener(), panel.BASE + "/manage/168020b4/danger/",
                           {"action": "delete", "csrf_token": "t"},
                           permitted_actions=permitted)
            self.assertIn("never permitted", str(raised.exception))

    def test_the_danger_path_still_passes_the_path_guard(self) -> None:
        # It posts fine because the path names no verb; the field fence is what
        # stops it, and that is why the tool must not rely on `refuse_path`.
        self.assertIsNone(panel.refuse_path("/manage/168020b4/danger/"))

    def test_the_danger_fields_are_in_the_permitted_set(self) -> None:
        for name in ("os", "want_snp", "want_fde", "want_measured",
                     "ssh_key", "confirm", "new_password", "new_password_confirm"):
            self.assertIn(name, panel.POST_FIELDS)


class SingleIdTests(FencedCase):
    """Rule (a): one id, exact match, and no widening by --allow-server."""

    def test_the_one_id_is_the_target(self) -> None:
        self.assertIsNone(panel.recycle_target(SERVER))

    def test_another_id_is_refused(self) -> None:
        self.assertIn("not the allow-list", panel.recycle_target(OTHER) or "")

    def test_an_id_is_not_case_folded_or_prefixed(self) -> None:
        self.assertIsNotNone(panel.recycle_target(SERVER.upper()))
        self.assertIsNotNone(panel.recycle_target(SERVER[:6]))

    def test_a_file_with_more_than_one_id_refuses_the_verb(self) -> None:
        self.id_file.write_text(SERVER + "\n" + OTHER + "\n", encoding="utf-8")

        why = panel.recycle_target(SERVER)

        self.assertIn("exactly one", why or "")
        self.assertIn(OTHER, why or "")

    def test_an_empty_or_missing_file_refuses(self) -> None:
        self.id_file.unlink()
        self.assertIn("exactly one", panel.recycle_target(SERVER) or "")

    def test_the_cli_refuses_to_widen_the_destructive_verb(self) -> None:
        posts: list = []
        code, _, err = self.run_tool(self.argv("--allow-server", OTHER), posts=posts)

        self.assertEqual(code, 2)
        self.assertIn("--allow-server", err)
        self.assertEqual(posts, [])

    def test_a_machine_outside_the_single_id_never_reaches_the_form(self) -> None:
        posts: list = []
        code, _, err = self.run_tool(
            ["--reinstall", "--server", OTHER, "--os", "debian13", "--want-snp",
             "--want-fde", "--want-measured", "--ssh-key-file", str(self.key_file)],
            posts=posts)

        self.assertEqual(code, 2)
        self.assertIn(OTHER, err)
        self.assertEqual(posts, [])
        self.assertEqual(self.lines(), [], "a refused id is not an attempt")


class DryRunTests(FencedCase):
    """`--apply` alone is a dry run; `--confirm` is the second flag."""

    def test_a_bare_reinstall_is_a_dry_run(self) -> None:
        posts: list = []
        code, out, _ = self.run_tool(self.argv(), posts=posts)

        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertIn("debian13", out)
        self.assertIn("ERASE", out)
        self.assertIn("odile-uki-test", out)
        self.assertEqual(posts, [])

    def test_the_dry_run_names_the_pair_it_would_lose(self) -> None:
        self.run_tool(self.argv())

        out = self.lines()[0]
        self.assertIn(f"boot={BOOT_URL}", out)
        self.assertIn(f"boot_sha256={BOOT_SHA}", out)
        self.assertIn(f"disk={DISK_URL}", out)
        self.assertIn(f"disk_sha256={DISK_SHA}", out)

    def test_the_audit_line_carries_the_pre_write_build_fields(self) -> None:
        self.run_tool(self.argv())

        lines = self.lines()
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ")
        self.assertIn("account=lkbxu6i9", line)
        self.assertIn(f"REINSTALL server={SERVER}", line)
        self.assertIn("os=debian13", line)
        self.assertIn("snp=1", line)
        self.assertIn("fde=1", line)
        self.assertIn("measured=1", line)
        self.assertIn("key=odile-uki-test", line)
        self.assertIn("route=panel/danger", line)
        self.assertIn("apply=0", line)

    def test_apply_without_confirm_is_a_usage_error(self) -> None:
        posts: list = []
        code, _, err = self.run_tool(self.argv("--apply"), posts=posts)

        self.assertEqual(code, 2)
        self.assertIn("--confirm", err)
        self.assertEqual(posts, [])
        self.assertEqual(self.lines(), [])

    def test_the_build_fields_have_no_defaults(self) -> None:
        for missing in ("--os", "--want-snp", "--want-fde", "--want-measured"):
            argv = self.argv()
            if missing == "--os":
                index = argv.index("--os")
                del argv[index:index + 2]
            else:
                argv.remove(missing)
            posts: list = []
            code, _, err = self.run_tool(argv, posts=posts)
            self.assertEqual(code, 2, missing)
            self.assertIn(missing, err)
            self.assertEqual(posts, [], missing)

    def test_the_ssh_key_comes_from_a_file(self) -> None:
        posts: list = []
        argv = self.argv()
        index = argv.index("--ssh-key-file")
        del argv[index:index + 2]
        code, _, err = self.run_tool(argv, posts=posts)

        self.assertEqual(code, 2)
        self.assertIn("--ssh-key-file", err)
        self.assertEqual(posts, [])

    def test_a_missing_key_file_is_a_usage_error(self) -> None:
        code, _, err = self.run_tool(
            ["--reinstall", "--server", SERVER, "--os", "debian13", "--want-snp",
             "--want-fde", "--want-measured", "--ssh-key-file", "/nonexistent/key.pub"])

        self.assertEqual(code, 2)
        self.assertIn("/nonexistent/key.pub", err)

    def test_there_is_no_key_or_erase_argument(self) -> None:
        # Rule (b) and (d): ERASE is typed by the tool and the key never rides
        # on argv, so neither exists as an option.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            panel.main(["--help"])
        self.assertNotIn("--erase", out.getvalue())
        self.assertNotIn("--ssh-key ", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            panel.main(self.argv("--confirm", "DELETE"))


class ApplyTests(FencedCase):
    """The real write: exact fields, line before POST, control required."""

    def test_the_post_carries_the_recorded_fields_exactly(self) -> None:
        posts: list = []
        code, out, _ = self.run_tool(self.argv("--apply", "--confirm"), posts=posts)

        self.assertEqual(code, 0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["url"], panel.BASE + f"/manage/{SERVER}/danger/")
        self.assertEqual(posts[0]["fields"], {
            "csrf_token": "tok-danger",
            "os": "debian13",
            "want_snp": "1",
            "want_fde": "1",
            "want_measured": "1",
            "ssh_key": KEY_TEXT,
            "confirm": "ERASE",
            "action": "reinstall",
        })
        self.assertIn("Reinstall queued.", out)

    def test_the_line_is_written_before_the_post(self) -> None:
        posts: list = []
        self.run_tool(self.argv("--apply", "--confirm"), posts=posts)

        lines = self.lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(f"REINSTALL server={SERVER}", lines[0])
        self.assertIn("apply=1", lines[0])
        self.assertEqual(posts[0]["audit_lines_before"], 1,
                         "the REINSTALL line must already be on disk when the form posts")

    def test_the_tool_types_erase_even_when_the_page_changed(self) -> None:
        posts: list = []
        code, _, err = self.run_tool(self.argv("--apply", "--confirm"),
                                     danger=DANGER_NO_CONTROL, posts=posts)

        self.assertEqual(code, 1)
        self.assertIn("Erase and reinstall", err)
        self.assertEqual(posts, [], "a changed page must not be posted at")

    def test_a_flash_refusal_fails_the_write(self) -> None:
        posts: list = []
        code, _, err = self.run_tool(
            self.argv("--apply", "--confirm"), posts=posts,
            bodies=['<div class="bb-flash err">Invalid request. Reload the page and try again.</div>'])

        self.assertEqual(code, 1)
        self.assertIn("refused", err)

    def test_a_machine_from_another_account_is_refused(self) -> None:
        posts: list = []
        with mock.patch.object(panel, "owned_servers", lambda opener: frozenset({OTHER})):
            code, _, _ = self.run_tool(self.argv("--apply", "--confirm"), posts=posts)

        self.assertEqual(code, 2)
        self.assertEqual(posts, [])

    def test_a_post_after_a_reinstall_names_no_pair(self) -> None:
        self.run_tool(self.argv(), admin=ADMIN_EMPTY)

        line = self.lines()[0]
        self.assertIn("boot=none", line)
        self.assertIn("disk=none", line)


class WaitTests(FencedCase):
    """The rebuild is 1 m 32 s; the next write must wait for `Running`."""

    def test_the_wait_follows_the_header_to_running(self) -> None:
        seen: list[str] = []

        def fake_get(opener, url: str) -> str:
            if url.rstrip("/").endswith("settings"):
                return SETTINGS_HTML
            if url.rstrip("/").endswith("dashboard"):
                return DASHBOARD_HTML
            if "/danger/" in url:
                return DANGER_HTML
            if "/admin/" in url:
                return ADMIN_HTML
            seen.append(url)
            return ("<header><span>Reinstalling</span></header>" if len(seen) == 1
                    else "<header><span>Running</span></header>")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(panel, "get", fake_get), \
                mock.patch.object(panel, "post", return_value="<p>queued</p>"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = panel.write_reinstall(None, SERVER, "debian13", True, True, True,
                                         str(self.key_file), apply=True, wait=5, interval=0)

        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 2, seen)
        self.assertIn("reinstalling", out.getvalue())
        self.assertIn("Running", out.getvalue())

    def test_a_machine_that_never_returns_fails(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(panel, "get", side_effect=lambda opener, url: (
                SETTINGS_HTML if url.rstrip("/").endswith("settings") else
                DASHBOARD_HTML if url.rstrip("/").endswith("dashboard") else
                DANGER_HTML if "/danger/" in url else
                ADMIN_HTML if "/admin/" in url else
                "<header><span>Reinstalling</span></header>")), \
                mock.patch.object(panel, "post", return_value="<p>queued</p>"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = panel.write_reinstall(None, SERVER, "debian13", True, True, True,
                                         str(self.key_file), apply=True, wait=0.5, interval=0)

        self.assertEqual(code, 1)
        self.assertIn("Running", err.getvalue())


class ImagePairTests(unittest.TestCase):
    """The line must name the pair the write is about to lose."""

    def test_the_pair_is_read_from_the_admin_page(self) -> None:
        pairs = panel.image_pairs(ADMIN_HTML)

        self.assertEqual(pairs["boot_url"], BOOT_URL)
        self.assertEqual(pairs["boot_sha256"], BOOT_SHA)
        self.assertEqual(pairs["disk_url"], DISK_URL)
        self.assertEqual(pairs["disk_sha256"], DISK_SHA)

    def test_a_reinstalled_machine_has_no_pair(self) -> None:
        pairs = panel.image_pairs(ADMIN_EMPTY)

        self.assertEqual(pairs, {"boot_url": "", "boot_sha256": "",
                                 "disk_url": "", "disk_sha256": ""})

    def test_a_page_without_the_sections_is_not_a_crash(self) -> None:
        self.assertEqual(panel.image_pairs("<p>nothing here</p>")["boot_url"], "")


class RecycleTests(FencedCase):
    """The composite: console, reinstall, boot image, disk image — in that order."""

    def setUp(self) -> None:
        super().setUp()
        # The boot-image read-back after the disk write needs the account's API
        # key; a fixture keeps the tests off /root/.odile.
        self.api_key = Path(self.dir.name) / "servury_reseller_key_lkbxu6i9"
        self.api_key.write_text("k" * 64 + "\n", encoding="utf-8")

    def recycle_argv(self, *extra: str) -> list[str]:
        line = Path(self.dir.name) / "cmdline.txt"
        line.write_text("root=/dev/mapper/root_crypt ro console=ttyS0,115200", encoding="utf-8")
        return [
            "--recycle", "--server", SERVER, "--os", "debian13",
            "--want-snp", "--want-fde", "--want-measured",
            "--ssh-key-file", str(self.key_file),
            "--boot-url", BOOT_URL, "--boot-sha256", BOOT_SHA,
            "--cmdline-file", str(line),
            "--disk-url", DISK_URL, "--disk-sha256", DISK_SHA, "--format", "raw",
            "--key-file", str(self.api_key),
            *extra,
        ]

    def run_recycle(self, argv: list[str], codes: list[int] | None = None
                    ) -> tuple[int, list[str], str]:
        calls: list[str] = []
        queue = list(codes or [])

        def step(name: str):
            def fake(*args, **kwargs):
                calls.append(name)
                return queue.pop(0) if queue else 0
            return fake

        out = io.StringIO()
        with mock.patch.object(panel, "console", step("console")), \
                mock.patch.object(panel, "write_reinstall", step("reinstall")), \
                mock.patch.object(panel, "write_boot_image", step("boot")), \
                mock.patch.object(panel, "write_disk", step("disk")), \
                mock.patch.object(panel, "ensure_boot_image", step("readback")), \
                contextlib.redirect_stdout(out):
            code = panel.main(argv)
        return code, calls, out.getvalue()

    def test_a_dry_run_plans_every_write_and_posts_nothing(self) -> None:
        code, calls, out = self.run_recycle(self.recycle_argv())

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["console", "reinstall", "boot", "disk"])
        self.assertIn("dry run", out)
        self.assertIn("would read ?action=boot-image back", out)

    def test_the_apply_order_is_console_reinstall_boot_disk(self) -> None:
        # The read-back is the fifth step, after the disk write and not before it.
        code, calls, _ = self.run_recycle(self.recycle_argv("--apply", "--confirm"))

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["console", "reinstall", "boot", "disk", "readback"])

    def test_a_widened_allow_list_refuses_before_the_console_write(self) -> None:
        # Round-1 review: the console arm was the composite's first write and
        # reached the panel before write_reinstall() ran its own fence. The
        # host's resting allow-list names two ids, so this is the live shape.
        self.id_file.write_text(SERVER + "\n" + OTHER + "\n", encoding="utf-8")
        posts: list = []
        code, _, err = self.run_tool(self.recycle_argv("--apply", "--confirm"), posts=posts)

        self.assertEqual(code, 2)
        self.assertIn("exactly one", err)
        self.assertEqual(posts, [], "the fence holds before the console is armed")
        self.assertEqual(self.lines(), [], "a refused id is not an attempt")

    def test_a_tenant_on_a_widened_list_is_never_armed(self) -> None:
        # The second machine (`OTHER`) *is* on the shared allow-list, so the
        # CLI's membership gate lets it through; the composite's own fence must
        # be what stops it, before any POST leaves.
        self.id_file.write_text(SERVER + "\n" + OTHER + "\n", encoding="utf-8")
        argv = self.recycle_argv("--apply", "--confirm")
        argv[argv.index("--server") + 1] = OTHER
        posts: list = []
        with mock.patch.object(panel, "owned_servers",
                               lambda opener: frozenset({SERVER, OTHER})):
            code, _, err = self.run_tool(argv, posts=posts)

        self.assertEqual(code, 2)
        self.assertIn("exactly one", err)
        self.assertEqual(posts, [])
        self.assertEqual(self.lines(), [])

    def test_the_sequence_stops_at_the_first_failure(self) -> None:
        code, calls, _ = self.run_recycle(self.recycle_argv("--apply", "--confirm"),
                                          codes=[0, 1])

        self.assertEqual(code, 1)
        self.assertEqual(calls, ["console", "reinstall"])

    def test_a_recycle_without_the_disk_pair_is_a_usage_error(self) -> None:
        argv = self.recycle_argv("--apply", "--confirm")
        index = argv.index("--disk-url")
        del argv[index:index + 2]
        code, calls, _ = self.run_recycle(argv)

        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_the_boot_sha_must_be_64_hex(self) -> None:
        argv = self.recycle_argv("--apply", "--confirm")
        argv[argv.index("--boot-sha256") + 1] = "f7bc0ceb"
        code, calls, _ = self.run_recycle(argv)

        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_the_cmdline_is_passed_from_the_file(self) -> None:
        seen: dict = {}

        def fake_boot(opener, server, url, sha, cmdline, apply, **kwargs):
            seen.update(url=url, sha=sha, cmdline=cmdline, apply=apply)
            return 0

        with mock.patch.object(panel, "console", lambda *a, **k: 0), \
                mock.patch.object(panel, "write_reinstall", lambda *a, **k: 0), \
                mock.patch.object(panel, "write_disk", lambda *a, **k: 0), \
                mock.patch.object(panel, "ensure_boot_image", lambda *a, **k: 0), \
                mock.patch.object(panel, "write_boot_image", fake_boot):
            code = panel.main(self.recycle_argv("--apply", "--confirm"))

        self.assertEqual(code, 0)
        self.assertEqual(seen["cmdline"], "root=/dev/mapper/root_crypt ro console=ttyS0,115200")
        self.assertEqual(seen["sha"], BOOT_SHA)
        self.assertTrue(seen["apply"])

    def test_the_line_is_refused_from_argv(self) -> None:
        # The line carries the per-machine seed token, and argv is world-readable
        # in `ps` (the tool's own `ssh_keys_from` docstring). `--recycle` takes
        # it from --cmdline-file only, and the refusal lands before the fence,
        # the console arm or any POST.
        argv = self.recycle_argv("--apply", "--confirm")
        index = argv.index("--cmdline-file")
        del argv[index:index + 2]
        argv += ["--cmdline", "SECRET-ON-ARGV-LINE"]
        posts: list = []
        code, _, err = self.run_tool(argv, posts=posts)

        self.assertEqual(code, 2)
        self.assertIn("--cmdline-file", err)
        self.assertIn("argv is world-readable", err)
        self.assertEqual(posts, [])
        self.assertEqual(self.lines(), [], "a refused line is not an attempt")

    def test_both_line_flags_are_still_refused(self) -> None:
        code, _, err = self.run_tool(self.recycle_argv("--cmdline", "x"))

        self.assertEqual(code, 2)
        self.assertIn("not both", err)

    def test_an_empty_key_file_refuses_before_the_console_write(self) -> None:
        # The read-back runs after the disk write, so an empty key discovered
        # there would spend the erase and the reinstall first. The pre-flight
        # reads the file, not only its name.
        self.api_key.write_text("\n", encoding="utf-8")
        posts: list = []
        code, _, err = self.run_tool(self.recycle_argv("--apply", "--confirm"), posts=posts)

        self.assertEqual(code, 2)
        self.assertIn("empty", err)
        self.assertIn("read-back", err)
        self.assertEqual(posts, [])
        self.assertEqual(self.lines(), [], "a refused key is not an attempt")


# The line the recycle passes to the attach; the same string the file holds.
CMDLINE = "root=/dev/mapper/root_crypt ro console=ttyS0,115200"
# `GET ?action=boot-image&id=…` answered this after the disk write completed on
# 2026-11-20T10:36:14Z (t_556631d6's evidence, recycle.txt): the field the
# attach had just set was gone and the machine had started the vendor release.
LOST = {"next_start": "release", "release": "2026.11.2", "image": None,
        "download": {"state": "none"}}


def boot_state(url: str = BOOT_URL, sha256: str = BOOT_SHA, cmdline: str = CMDLINE,
               next_start: str = "custom") -> dict:
    """A `boot_image` payload shaped like the live GET (measured 2026-11-20)."""
    image = None if next_start != "custom" else {
        "url": url, "sha256": sha256, "size": 120480216, "type": "pe",
        "initrd_url": None, "initrd_sha256": None, "initrd_size": None,
        "cmdline": cmdline,
    }
    return {"next_start": next_start, "release": "2026.11.2", "image": image,
            "download": {"state": "ready" if image else "none"}}


class _FakeHttpResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *exc) -> bool:  # noqa: ANN002 - context manager protocol
        return False


class BootImageStateTests(unittest.TestCase):
    """The read-back's two halves: the vendor's completion fact and the pair.

    The disk write returns before the image is written, so a read straight after
    the POST is a pre-write read that says nothing. The panel's own `Written`
    stamp in the Disk section is the completion record (the SPIKE measured it at
    18:51:52Z for a write submitted at 18:50:35Z), and `?action=boot-image` is
    the state that says what the next start is.
    """

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.key = Path(self.dir.name) / "servury_reseller_key_lkbxu6i9"
        self.key.write_text("k" * 64 + "\n", encoding="utf-8")

    def test_the_vendors_written_stamp_is_read(self) -> None:
        self.assertEqual(panel.disk_written_at(ADMIN_HTML), "2026-11-19T13:18:08Z")

    def test_a_page_without_a_disk_stamp_is_empty(self) -> None:
        self.assertEqual(panel.disk_written_at(ADMIN_EMPTY), "")
        self.assertEqual(panel.disk_written_at(""), "")

    def test_a_matching_state_is_no_reason_to_write(self) -> None:
        self.assertIsNone(panel.boot_image_matches(boot_state(), BOOT_URL, BOOT_SHA, CMDLINE))

    def test_the_release_is_a_reason(self) -> None:
        why = panel.boot_image_matches(LOST, BOOT_URL, BOOT_SHA, CMDLINE)

        self.assertIsNotNone(why)
        self.assertIn("release", why)

    def test_another_sha_is_a_reason(self) -> None:
        why = panel.boot_image_matches(boot_state(), BOOT_URL, "ab" * 32, CMDLINE)

        self.assertIsNotNone(why)
        self.assertIn("abababab", why)

    def test_another_line_is_a_reason(self) -> None:
        why = panel.boot_image_matches(boot_state(cmdline="console=ttyS0"), BOOT_URL,
                                       BOOT_SHA, CMDLINE)

        self.assertIsNotNone(why)
        self.assertIn("line", why)

    def test_a_null_image_is_a_reason_not_a_crash(self) -> None:
        state = {"next_start": "custom", "image": None}

        self.assertIsNotNone(panel.boot_image_matches(state, BOOT_URL, BOOT_SHA, CMDLINE))
        self.assertIsNotNone(panel.boot_image_matches({}, BOOT_URL, BOOT_SHA, CMDLINE))

    def test_the_api_payload_is_read_with_the_key_from_the_file(self) -> None:
        seen: dict = {}

        def fake_urlopen(request, timeout=None):  # noqa: ARG001 - the tool's timeout
            seen["url"] = request.full_url
            seen["headers"] = {name.lower(): value for name, value in request.header_items()}
            return _FakeHttpResponse(
                '{"success":true,"data":{"boot_image":' + json.dumps(LOST) + "}}")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            state = panel.read_boot_image(self.key, SERVER)

        self.assertEqual(state, LOST)
        self.assertIn("action=boot-image", seen["url"])
        self.assertIn("id=" + SERVER, seen["url"])
        self.assertEqual(seen["headers"]["x-reseller-key"], "k" * 64)

    def test_the_api_s_own_sentence_is_the_error(self) -> None:
        def fake_urlopen(request, timeout=None):  # noqa: ARG001
            return _FakeHttpResponse('{"success":false,"error":"Server not found"}')

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as caught:
                panel.read_boot_image(self.key, SERVER)

        self.assertIn("Server not found", str(caught.exception))

    def test_a_page_instead_of_json_is_a_sentence(self) -> None:
        # An expired session serves the login page, not the API's JSON. That is
        # a ValueError from json.load; the caller must read a sentence, not a
        # traceback, because this read happens after the disk write.
        def fake_urlopen(request, timeout=None):  # noqa: ARG001
            return _FakeHttpResponse("<html>sign in</html>")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as caught:
                panel.read_boot_image(self.key, SERVER)

        self.assertIn("no JSON", str(caught.exception))


class ReadBackTests(FencedCase):
    """The composite does not trust its own attach print.

    Measured twice on 2026-11-20 (t_556631d6, evidence on t_6d956aee): the
    attach printed success at 10:34:30Z, the disk write completed at 10:36:14Z,
    and `?action=boot-image` then said `next_start: release`, `image: null` —
    the machine was sitting at the vendor release's unlock prompt. The composite
    therefore waits for the write's own `Written` stamp, reads the state back,
    and re-attaches the same URL, SHA-256 and line when the field is gone.
    """

    def setUp(self) -> None:
        super().setUp()
        self.key = Path(self.dir.name) / "servury_reseller_key_lkbxu6i9"
        self.key.write_text("k" * 64 + "\n", encoding="utf-8")
        self.line_file = Path(self.dir.name) / "cmdline.txt"
        self.line_file.write_text(CMDLINE + "\n", encoding="utf-8")

    def recycle_argv(self, *extra: str) -> list[str]:
        return [
            "--recycle", "--server", SERVER, "--os", "debian13",
            "--want-snp", "--want-fde", "--want-measured",
            "--ssh-key-file", str(self.key_file),
            "--boot-url", BOOT_URL, "--boot-sha256", BOOT_SHA,
            "--cmdline-file", str(self.line_file),
            "--disk-url", DISK_URL, "--disk-sha256", DISK_SHA, "--format", "raw",
            "--key-file", str(self.key),
            *extra,
        ]

    def run_readback(self, argv: list[str], states: list[dict], *,
                     written: str | None = "2026-11-20T10:36:14Z"
                     ) -> tuple[int, list, str, str]:
        calls: list[tuple] = []
        queue = list(states)

        def step(name: str):
            def fake(*args, **kwargs):
                calls.append((name, args, kwargs))
                return 0
            return fake

        def fake_written(opener, server, since, **kwargs):  # noqa: ARG001
            calls.append(("wait", (server, since), kwargs))
            return written

        def fake_read(key_file, server, **kwargs):
            calls.append(("read", (key_file, server), kwargs))
            return queue.pop(0)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(panel, "console", step("console")), \
                mock.patch.object(panel, "write_reinstall", step("reinstall")), \
                mock.patch.object(panel, "write_boot_image", step("boot")), \
                mock.patch.object(panel, "write_disk", step("disk")), \
                mock.patch.object(panel, "power", step("power")), \
                mock.patch.object(panel, "wait_disk_written", fake_written), \
                mock.patch.object(panel, "read_boot_image", fake_read), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = panel.main(argv)
        return code, calls, out.getvalue(), err.getvalue()

    @staticmethod
    def names(calls: list[tuple]) -> list[str]:
        return [name for name, *_ in calls]

    def test_the_pair_present_after_the_write_is_not_rewritten(self) -> None:
        code, calls, out, err = self.run_readback(
            self.recycle_argv("--apply", "--confirm"), [boot_state()])

        self.assertEqual(code, 0, err)
        self.assertEqual(self.names(calls),
                         ["console", "reinstall", "boot", "disk", "wait", "read"])
        self.assertIn("no re-attach", out)

    def test_a_lost_field_is_reattached_once_with_the_same_pair_and_line(self) -> None:
        code, calls, out, err = self.run_readback(
            self.recycle_argv("--apply", "--confirm"), [LOST, boot_state()])

        self.assertEqual(code, 0, err)
        self.assertEqual(self.names(calls),
                         ["console", "reinstall", "boot", "disk", "wait", "read",
                          "boot", "read", "power"])
        reattach = calls[6]
        self.assertEqual(reattach[1][1:5], (SERVER, BOOT_URL, BOOT_SHA, CMDLINE),
                         "the re-attach carries the same pair and the line from the file")
        self.assertTrue(reattach[2].get("apply"))
        self.assertEqual(calls[8][1][:3], (mock.ANY, SERVER, "restart"),
                         "a lost field costs a plain restart, not a second disk write")
        self.assertIn("cleared the boot-image field", out)

    def test_a_reattach_that_does_not_stick_fails(self) -> None:
        code, calls, out, err = self.run_readback(
            self.recycle_argv("--apply", "--confirm"), [LOST, LOST])

        self.assertEqual(code, 1)
        self.assertIn("did not stick", err)
        self.assertNotIn("power", self.names(calls),
                         "a field that is still gone is not a machine to restart")
        self.assertNotIn("recycle complete", out)

    def test_the_disk_write_completion_is_waited_for(self) -> None:
        # The POST returns before the image is written; a read straight after it
        # is a pre-write read. No `Written` stamp in `--wait` seconds fails the
        # run rather than calling a pre-write state "survived".
        code, calls, out, err = self.run_readback(
            self.recycle_argv("--apply", "--confirm"), [boot_state()], written=None)

        self.assertEqual(code, 1)
        self.assertIn("Written", err)
        self.assertNotIn("read", self.names(calls))

    def test_the_readback_runs_after_the_disk_write_and_before_the_last_line(self) -> None:
        code, calls, out, _ = self.run_readback(
            self.recycle_argv("--apply", "--confirm"), [boot_state()])

        self.assertEqual(code, 0)
        self.assertEqual(self.names(calls)[-3:], ["disk", "wait", "read"])
        self.assertIn("recycle complete", out.strip().splitlines()[-1])

    def test_a_missing_key_file_refuses_before_the_console_write(self) -> None:
        # The read-back is part of the composite, so a run that cannot read it
        # must refuse before it erases anything — not after the disk write.
        argv = self.recycle_argv("--apply", "--confirm")
        argv[argv.index("--key-file") + 1] = str(Path(self.dir.name) / "absent")
        code, calls, out, err = self.run_readback(argv, [])

        self.assertEqual(code, 2)
        self.assertIn("read-back", err)
        self.assertEqual(calls, [])
        self.assertEqual(self.lines(), [], "a refused run posts nothing and logs nothing")

    def test_a_dry_run_plans_the_readback_and_needs_no_key(self) -> None:
        argv = self.recycle_argv()
        argv[argv.index("--key-file") + 1] = str(Path(self.dir.name) / "absent")
        code, calls, out, err = self.run_readback(argv, [])

        self.assertEqual(code, 0, err)
        self.assertEqual(self.names(calls), ["console", "reinstall", "boot", "disk"])
        self.assertIn("read ?action=boot-image back", out)

    def test_a_read_error_after_the_write_is_a_sentence_not_a_traceback(self) -> None:
        # The read-back runs after the disk write, so a panel/API hiccup must
        # end the run with a sentence and exit 1, not a JSONDecodeError.
        err = io.StringIO()
        with mock.patch.object(panel, "wait_disk_written",
                               return_value="2026-11-20T10:36:14Z"), \
                mock.patch.object(panel, "read_boot_image",
                                  side_effect=json.JSONDecodeError("Expecting value",
                                                                   "<html>", 0)), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = panel.ensure_boot_image(None, SERVER, BOOT_URL, BOOT_SHA, CMDLINE,
                                           self.key, wait=1, interval=0)

        self.assertEqual(code, 1)
        self.assertIn("read-back failed", err.getvalue())

    def test_a_page_error_while_waiting_is_a_sentence_not_a_traceback(self) -> None:
        err = io.StringIO()
        with mock.patch.object(panel, "wait_disk_written",
                               side_effect=urllib.error.URLError("timed out")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = panel.ensure_boot_image(None, SERVER, BOOT_URL, BOOT_SHA, CMDLINE,
                                           self.key, wait=1, interval=0)

        self.assertEqual(code, 1)
        self.assertIn("Written", err.getvalue())


if __name__ == "__main__":
    unittest.main()
