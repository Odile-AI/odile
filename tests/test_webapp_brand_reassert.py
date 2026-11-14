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


class UnitFileTests(unittest.TestCase):
    """The trigger is the bundle changing; the boot is not the trigger."""

    def setUp(self):
        self.path_unit = (IMAGE / "odile-webapp-brand.path").read_text(encoding="utf-8")
        self.service = (IMAGE / "odile-webapp-brand.service").read_text(encoding="utf-8")

    def test_the_path_unit_watches_the_built_shell(self):
        self.assertIn(
            "PathChanged=__TENANT_HOME__/.hermes/hermes-agent/apps/desktop/dist-webapp/index.html",
            self.path_unit)
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


if __name__ == "__main__":
    unittest.main()
