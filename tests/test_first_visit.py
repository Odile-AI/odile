#!/usr/bin/python3
"""First-visit sign-in and recovery code (ODL-457).

These are medium tests: a real first_visit.py process on a free loopback port,
a real tenant ``.env`` in a temp directory, and HTTP over a socket. The
behaviour is what the card promises — a valid token renders the page once, a
reused token is refused, the POST writes the basic-auth hash on the machine,
and the recovery code resets a lost sign-in.

Nothing here reads /var/lib/odile or a live tenant. The page is served from the
shipped ``web/first-visit.html``, not a fixture, so a change to the page is
tested here and by the brand lint.

    /usr/bin/python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "installer" / "first_visit.py"
PAGE = ROOT / "web" / "first-visit.html"
INSTALLER = ROOT / "installer" / "odile_install.py"
# The five files the installer pins and the page server answers (ODL-505), in
# the order the tenant route table names them.
ICON_FILES = (
    ("favicon.ico", "image/x-icon"),
    ("icon-32.png", "image/png"),
    ("icon-192.png", "image/png"),
    ("icon-512.png", "image/png"),
    ("apple-touch-icon.png", "image/png"),
)
ICON_CACHE = "public, max-age=31536000, immutable"

sys.path.insert(0, str(ROOT / "installer"))
import first_visit as fv  # noqa: E402

PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "a-different-long-password-0002"
CODE_RE = re.compile(r'<p class="code" id="code">([^<]+)</p>')
# The one-time download token the done pages carry (ODL-579): 32 bytes, hex,
# hashed in the state, single use, ten minutes.
DOWNLOAD_RE = re.compile(r'href="/first-visit/recovery\?d=([0-9a-f]{64})"')


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def hermes_agent_root() -> Path | None:
    """The Hermes checkout whose dashboard basic-auth the hash must satisfy."""
    candidates = [os.environ.get("ODILE_HERMES_AGENT_DIR", "")]
    candidates += ["/root/.hermes/hermes-agent", "/home/odile/.hermes/hermes-agent"]
    for raw in candidates:
        if not raw:
            continue
        root = Path(raw)
        if (root / "plugins" / "dashboard_auth" / "basic" / "__init__.py").is_file():
            return root
    return None


def tenant_env(username: str = "odile", password_hash: str | None = None) -> str:
    """The installer's three-key allowlist, with a placeholder hash."""
    return (
        "# Odile tenant .env — an allowlist, not a copy of a seat .env.\n"
        "# Exactly these three keys.\n"
        f"HERMES_DASHBOARD_BASIC_AUTH_USERNAME={username}\n"
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=%s\n"
        % (password_hash or "scrypt$16384$8$1$AAAA$BBBB")
        + "HERMES_DASHBOARD_BASIC_AUTH_SECRET=c2VjcmV0LXBsYWNlaG9sZGVy\n"
    )


BACKUP_PASSWORD = "backup-password-0123456789abcdef"
PLACEHOLDER = "(pending — filled in when you download this file on the first-visit page)"
# A9's decided v3.9 row (docs/product/CLAIMS-RECUT-STRICT.md §2), verbatim.
# The deck lands in ODL-462; until then the first-visit page quotes this exact
# sentence, and the old v3.7 copy ("we hold neither") is cut as unprovable.
A9_DECIDED = (
    "A recovery code and a recovery file are the way back: the code resets your "
    "sign-in, the file opens your backup. Your first visit gives you both; keep "
    "them off the machine. While the machine is still running, the agent can "
    "show it again."
)
# D-6 (docs/CLAIMS.md v3.11), the row that replaces A9's tail: the first-visit
# pages carry it verbatim (ODL-925/ODL-926).
D6_DECIDED = (
    "A recovery code and a recovery file are the way back: the code resets your "
    "sign-in, the file opens your backup. Your first visit gives you both; keep "
    "them off the machine. While the machine is still running, the agent can "
    "replace the code and show you the new one."
)
# D-20 (ODL-926): the stability rule; the reset page carries it verbatim and
# the done pages carry the allowed subset.
D20_DECIDED = (
    "Your recovery code works at both places until you replace it; using it "
    "does not spend it."
)
D20_SUBSET = "Your recovery code is unchanged; using it does not spend it."
# D-4 and D-5 (ODL-926): the one-code and lost-code facts, where a page says
# them.
D4_DECIDED = (
    "One recovery code resets both: your account password at the account page "
    "and your machine's sign-in on the machine. There is no emailed reset."
)
D5_DECIDED = (
    "If you lose your recovery code there is no other way back: you start a new "
    "account, and a backup is the only way to bring the files."
)
# The retiring wordings (ODL-579's single-use/rotation). No rendered page and
# no kit may carry them (ODL-926).
RETIRED = (
    "The code you used stops working; a new one is shown next.",
    "The code you use stops working; a new one is shown next.",
    "This is the only time the new code is shown. The page will not show it again.",
    "the agent can show it again",
    "using it there rotates it too",
)
# D-1 (docs/CLAIMS.md v3.11): the page's footer, verbatim.
D1_DECIDED = (
    "Your Odile password opens your machine and your account page. You chose it "
    "when you registered. It is the only app-layer gate: no 2FA in alpha.0."
)


def recovery_text(slug: str = "odl-test", fingerprint: str = PLACEHOLDER) -> str:
    """The file the installer writes, with the first-visit copy (ODL-471)."""
    return (
        "Odile recovery file\n"
        "===================\n"
        f"Tenant:              {slug}\n"
        "Instance:            srv-1\n"
        "Written:             2026-11-10T00:00:00Z\n"
        "\n"
        f"Backup password:     {BACKUP_PASSWORD}\n"
        f"Certificate SHA-256: {fingerprint}\n"
        "\n"
        "This file is the only copy of the backup password. Download it on this\n"
        "machine's first-visit page and keep it somewhere you can reach when the\n"
        "machine is gone. Ask your agent to show it again if you need it.\n"
        "\n"
        "Lose it and the encrypted backup cannot be opened; Odile holds no copy.\n"
    )


def openssl_fingerprint(path: Path) -> str:
    """The digest `openssl x509 -fingerprint -sha256` prints, normalised.

    This is the oracle for every test here: the value a browser shows in its
    certificate viewer is the same DER digest.
    """
    printed = subprocess.run(
        ["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    match = re.search(r"=\s*([0-9A-Fa-f:]{32,})", printed)
    assert match is not None, printed
    return "sha256:" + match.group(1).replace(":", "").lower()


def certificate_fixture(directory: Path, host: str) -> tuple[Path, str]:
    """Make a certificate for `host` in a Caddy-shaped store.

    `directory` is the store root (Caddy's certificates dir); the leaf lands
    at <root>/acme/<host>/<host>.crt. Returns the root and the sha256.
    """
    key = directory / f"{host}.key"
    cert = directory / f"{host}.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(key), "-out", str(cert), "-subj", f"/CN={host}"],
        check=True, capture_output=True,
    )
    store = directory / "acme" / host
    store.mkdir(parents=True)
    (store / f"{host}.crt").write_bytes(cert.read_bytes())
    return directory, openssl_fingerprint(cert)


def certificate_chain_fixture(directory: Path, host: str) -> tuple[Path, str, str]:
    """A store file shaped the way certmagic writes it: leaf, then issuer.

    Real Caddy stores carry the leaf certificate *and* the issuer chain in one
    .crt, for internal and ACME issuers alike. Returns the store root, the
    leaf's sha256 (the value a browser shows), and the digest over the
    concatenated DER of every block — the pre-fix value where its base64 decode
    happened to succeed (other chain lengths make it raise instead).
    """
    ca_key = directory / "issuer.key"
    ca_cert = directory / "issuer.crt"
    leaf_key = directory / f"{host}.key"
    leaf_csr = directory / f"{host}.csr"
    leaf_cert = directory / f"{host}.leaf.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(ca_key), "-out", str(ca_cert),
         "-subj", "/CN=Odile Test Issuer",
         "-addext", "basicConstraints=critical,CA:TRUE"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(leaf_key), "-out", str(leaf_csr), "-subj", f"/CN={host}"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "x509", "-req", "-in", str(leaf_csr), "-CA", str(ca_cert),
         "-CAkey", str(ca_key), "-CAcreateserial", "-days", "1", "-out", str(leaf_cert)],
        check=True, capture_output=True,
    )
    bundle = leaf_cert.read_bytes() + ca_cert.read_bytes()
    store = directory / "local" / host
    store.mkdir(parents=True)
    (store / f"{host}.crt").write_bytes(bundle)
    chained = "sha256:" + hashlib.sha256(
        ssl.PEM_cert_to_DER_cert(bundle.decode("utf-8"))).hexdigest()
    return directory, openssl_fingerprint(leaf_cert), chained


class FirstVisitHarness:
    """One service process, one temp guest root, one token.

    ``seeded`` is the B-once shape (ODL-925): the .env already holds the
    registration password's verifier and the state says ``signin_set_at``, so
    the page serves a login. The default is the classic install: no seed wrote
    a usable sign-in, so the visit chooses one and the page serves the setup.
    """

    def __init__(self, reload_command: str = "", username: str = "odile",
                 recovery: str | None = None, caddy_stores: list[Path] | None = None,
                 seeded: bool = False, push_command: str = "",
                 restore: dict | None = None, restore_command: str = ""):
        self.tmp = tempfile.TemporaryDirectory(prefix="odl457-")
        self.root = Path(self.tmp.name)
        self.hermes = self.root / "home" / "odile" / ".hermes"
        self.hermes.mkdir(parents=True)
        self.env = self.hermes / ".env"
        self.seeded = seeded
        self.env.write_text(
            tenant_env(username, fv.hash_password(PASSWORD) if seeded else None),
            encoding="utf-8")
        self.env.chmod(0o600)
        self.state = self.root / "etc" / "first-visit.json"
        self.recovery = self.root / "etc" / "recovery.txt"
        self.token = secrets.token_hex(32)
        state = fv.initialise_state(self.state, self.token)
        if seeded:
            state["signin_set_at"] = state["created_at"]
        if restore is not None:
            state["restore"] = dict(restore)
            state["signin_set_at"] = state["signin_set_at"] or state["created_at"]
        if seeded or restore is not None:
            fv.save_state(self.state, state)
        if recovery is not None:
            self.recovery.write_text(recovery, encoding="utf-8")
            self.recovery.chmod(0o600)
        # The icon root the installer writes (ODL-505), seeded from the
        # committed files so the route serves the bytes the release pins.
        self.icons = self.root / "usr" / "local" / "lib" / "odile" / "icons"
        self.icons.mkdir(parents=True)
        for name, _ctype in ICON_FILES:
            shutil.copyfile(ROOT / "web" / name, self.icons / name)
        self.reload_log = self.root / "reload.log"
        self.push_log = self.root / "push.log"
        self.port = free_port()
        command = [
            sys.executable, str(SERVICE),
            "--bind", "127.0.0.1", "--port", str(self.port),
            "--state", str(self.state), "--env-file", str(self.env),
            "--page", str(PAGE), "--recovery-file", str(self.recovery),
            "--icons", str(self.icons),
        ]
        for store in caddy_stores or []:
            command += ["--caddy-store", str(store)]
        if reload_command:
            command += ["--reload-command", reload_command]
        else:
            command += ["--reload-command", f"sh -c 'date >> {self.reload_log}'"]
        if push_command:
            command += ["--push-command", push_command]
        else:
            command += ["--push-command", f"sh -c 'date >> {self.push_log}'"]
        self.restore_log = self.root / "restore.log"
        if restore_command:
            command += ["--restore-command", restore_command]
        else:
            command += ["--restore-command", f"sh -c 'date >> {self.restore_log}'"]
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        for _ in range(100):
            try:
                status, _, _ = self.call("GET", "/health")
                if status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.close()
        raise AssertionError("the first-visit service did not start")

    # -- HTTP ------------------------------------------------------------

    def call(self, method: str, path: str, fields: dict[str, str] | None = None,
             body: bytes | None = None, headers: dict[str, str] | None = None):
        if fields is not None:
            body = "&".join("%s=%s" % (key, value) for key, value in fields.items()).encode()
        request_headers = dict(headers or {})
        if body:
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read().decode("utf-8", "replace")
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def set_signin(self, password: str = PASSWORD,
                   headers: dict[str, str] | None = None) -> tuple[int, dict, str]:
        """The classic setup POST: choose a sign-in. No token travels."""
        return self.call("POST", "/first-visit",
                         {"password": password, "password2": password},
                         headers=headers)

    def signin(self, password: str = PASSWORD,
               headers: dict[str, str] | None = None) -> tuple[int, dict, str]:
        """The B-once login POST: one password, verified against the .env."""
        return self.call("POST", "/first-visit", {"password": password},
                         headers=headers)

    def fetch(self, method: str, path: str) -> tuple[int, dict, bytes]:
        """One request whose body is bytes, not text: the icon route (ODL-505)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def download(self, token: str, headers: dict[str, str] | None = None):
        """Fetch the recovery file with the one-time download token (ODL-579).

        The code is never a bearer in a URL: the done page carries ``?d=`` and
        the code is only ever POSTed.
        """
        return self.call("GET", "/first-visit/recovery?d=" + token, headers=headers)

    def download_token(self, page: str) -> str:
        """The one-time download token the response carries, or a failure."""
        match = DOWNLOAD_RE.search(page)
        if match is None:
            raise AssertionError("the page carries no one-time download token")
        return match.group(1)

    def use_code(self, code: str, headers: dict[str, str] | None = None):
        """Present the code at the recovery page; the code is not spent."""
        return self.call("POST", "/first-visit/recovery", {"code": code},
                         headers=headers)

    def replace(self, password: str = PASSWORD,
                headers: dict[str, str] | None = None):
        """The signed-in replace action: the current sign-in and a new code."""
        return self.call("POST", "/first-visit/replace", {"password": password},
                         headers=headers)

    def paste(self, text: str):
        """Paste a whole recovery file: the field is URL-encoded, line breaks and all."""
        body = urlencode({"recovery": text}).encode("ascii")
        return self.call("POST", "/first-visit/restore", body=body)

    def restore_state(self) -> dict:
        """The state's restore block, as the page and the unit see it."""
        return fv.restore_block(self.load_state())

    def restore_starts(self) -> int:
        """How many times the page asked for the restore unit."""
        try:
            return len(self.restore_log.read_text(encoding="utf-8").splitlines())
        except OSError:
            return 0

    def push_requests(self) -> int:
        """How many times the service asked for the recovery push."""
        try:
            return len(self.push_log.read_text(encoding="utf-8").splitlines())
        except OSError:
            return 0

    # -- state -----------------------------------------------------------

    def load_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def env_text(self) -> str:
        return self.env.read_text(encoding="utf-8")

    def stored_hash(self) -> str:
        for line in self.env_text().splitlines():
            if line.startswith(fv.PASSWORD_KEY + "="):
                return line.split("=", 1)[1]
        raise AssertionError("the .env has no password hash")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.tmp.cleanup()


class HashFormatTests(unittest.TestCase):
    """The installer and the service must agree, or the sign-in is dead."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("odile_install_457", INSTALLER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.installer = module

    def test_the_installer_hash_verifies_in_the_service(self):
        encoded = self.installer.hash_password(PASSWORD)
        self.assertTrue(fv.verify_password(PASSWORD, encoded))
        self.assertFalse(fv.verify_password(PASSWORD + "x", encoded))

    def test_the_service_hash_verifies_in_the_installer(self):
        encoded = fv.hash_password(PASSWORD)
        self.assertTrue(self.installer.verify_password(PASSWORD, encoded))
        self.assertFalse(self.installer.verify_password("nope", encoded))

    def test_the_encoded_hash_carries_the_provider_parameters(self):
        scheme, n, r, p, salt, digest = fv.hash_password(PASSWORD).split("$")
        self.assertEqual((scheme, n, r, p), ("scrypt", "16384", "8", "1"))
        self.assertEqual(len(salt), 24)   # 16 bytes, base64
        self.assertEqual(len(digest), 44)  # 32 bytes, base64

    def test_a_malformed_hash_never_raises(self):
        for bad in ("", "scrypt", "scrypt$1$2$3$AAAA", "bcrypt$16384$8$1$AAAA$BBBB",
                    "scrypt$16384$8$1$!!!!$BBBB"):
            with self.subTest(bad=bad):
                self.assertFalse(fv.verify_password(PASSWORD, bad))

    def test_the_installer_writes_state_the_service_accepts(self):
        token = secrets.token_hex(32)
        body = self.installer.first_visit_state(token)
        state = fv.validate_state(json.loads(body))
        self.assertEqual(state["token_sha256"], hashlib.sha256(token.encode()).hexdigest())
        self.assertIsNone(state["used_at"])


class FirstVisitEnvTests(unittest.TestCase):
    """ODL-521: the boot's hash is a placeholder until the visit.

    The published image ships no usable sign-in, and the release installer runs
    with ``--first-visit`` on every boot. Before the page has set the owner's
    sign-in (``signin_set_at`` in the state) the hash is replaced by one nobody
    knows; after it, the owner's hash is kept byte for byte. The session secret
    is not this card's: it must be identical before and after every run.
    """

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("odile_install_521", INSTALLER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.installer = module

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="odl521-")
        self.addCleanup(self.tmp.cleanup)
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.layout = self.installer.Layout.scratch(
            root=Path(self.tmp.name), user=self.user, webapp_port=9130,
            public_url="https://odl-test.odile.fyi")
        self.layout.hermes_home.mkdir(parents=True)
        self.env = self.layout.hermes_home / ".env"
        self.secret = "bm90LXRoZS1pbWFnZS1zZWNyZXQ="

    def write_env(self, password_hash: str) -> None:
        self.env.write_text(
            "# Odile tenant .env — the bench image's bytes\n"
            "# odile-signin-placeholder: 1\n"
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=%s\n"
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=%s\n"
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET=%s\n"
            % (self.user, password_hash, self.secret), encoding="utf-8")
        self.env.chmod(0o600)

    def write_state(self, signin_set_at: str | None) -> None:
        self.layout.first_visit_state.parent.mkdir(parents=True, exist_ok=True)
        self.layout.first_visit_state.write_text(json.dumps({
            "version": 1, "token_sha256": "0" * 64,
            "created_at": "2026-11-13T00:00:00Z", "used_at": signin_set_at,
            "signin_set_at": signin_set_at, "recovery_hash": None,
            "recovery_issued_at": None, "recovery_used_at": None,
        }), encoding="utf-8")

    def stored(self) -> dict[str, str]:
        return self.installer.env_values(self.env.read_text(encoding="utf-8"))

    def ensure(self, dry: bool = False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.installer.ensure_env(
                self.layout, None, self.user, dry, first_visit=True)
        self.output = output.getvalue()
        return result

    def test_the_images_bench_hash_is_replaced_before_the_visit(self):
        self.write_env(self.installer.hash_password("odile-bench-not-a-secret"))
        self.write_state(None)
        changed, password = self.ensure()
        self.assertTrue(changed)
        self.assertIsNone(password)
        stored = self.stored()
        self.assertFalse(self.installer.verify_password(
            "odile-bench-not-a-secret",
            stored["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH"]))
        self.assertIn("placeholder until first visit", self.output)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    def test_every_boot_before_the_visit_rewrites_the_placeholder(self):
        self.write_env(self.installer.hash_password("odile-bench-not-a-secret"))
        self.write_state(None)
        self.ensure()
        first = self.stored()["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH"]
        self.ensure()
        second = self.stored()["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH"]
        self.assertNotEqual(first, second,
                            "a boot before the visit must not keep an earlier placeholder")
        self.assertFalse(self.installer.verify_password("odile-bench-not-a-secret", second))

    def test_the_session_secret_is_kept(self):
        """ODL-522: the machine's secret survives every --first-visit run.

        The first boot generates this machine's secret (install-stack.sh); the
        release installer runs with --first-visit on every boot and must keep
        it byte for byte, before and after the page sets the owner's sign-in.
        """
        self.write_env(self.installer.hash_password("odile-bench-not-a-secret"))
        self.write_state(None)
        self.ensure()
        self.assertEqual(self.stored()["HERMES_DASHBOARD_BASIC_AUTH_SECRET"], self.secret)

    def test_the_owners_hash_survives_the_next_boot(self):
        owner = self.installer.hash_password("the-owner-password-0001")
        self.write_env(owner)
        self.write_state("2026-11-13T00:10:00Z")
        changed, password = self.ensure()
        self.assertFalse(changed)
        self.assertIsNone(password)
        self.assertEqual(self.stored()["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH"], owner)
        self.assertEqual(self.stored()["HERMES_DASHBOARD_BASIC_AUTH_SECRET"], self.secret,
                         "the post-visit boot rotated the machine's session secret")

    def test_a_dry_run_says_it_would_rewrite_and_changes_nothing(self):
        self.write_env(self.installer.hash_password("odile-bench-not-a-secret"))
        self.write_state(None)
        before = self.env.read_text(encoding="utf-8")
        changed, _ = self.ensure(dry=True)
        self.assertTrue(changed)
        self.assertIn("would rewrite", self.output)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)


class LeafDerTests(unittest.TestCase):
    """The store file's first block is the pinned value; nothing after it."""

    def test_a_chain_returns_the_first_blocks_der_not_the_concatenation(self):
        with tempfile.TemporaryDirectory(prefix="odl471-der-") as tmp:
            store, leaf, chained = certificate_chain_fixture(Path(tmp), "odl-test.example")
            bundle = (store / "local" / "odl-test.example" /
                      "odl-test.example.crt").read_text(encoding="utf-8")
            self.assertEqual(leaf, "sha256:" + hashlib.sha256(
                fv.leaf_der(bundle)).hexdigest())
            self.assertNotEqual(leaf, chained)

    def test_prose_and_crlf_around_the_blocks_do_not_change_the_leaf(self):
        with tempfile.TemporaryDirectory(prefix="odl471-crlf-") as tmp:
            store, leaf, _chained = certificate_chain_fixture(Path(tmp), "odl-test.example")
            bundle = (store / "local" / "odl-test.example" /
                      "odl-test.example.crt").read_text(encoding="utf-8")
            hand_edited = "Bench store, do not ship.\r\n" + bundle.replace("\n", "\r\n")
            self.assertEqual(leaf, "sha256:" + hashlib.sha256(
                fv.leaf_der(hand_edited)).hexdigest())

    def test_no_certificate_block_returns_none(self):
        for pem in ("", "garbage\n",
                    "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"):
            self.assertIsNone(fv.leaf_der(pem), pem)


class HermesBasicAuthTests(unittest.TestCase):
    """The hash the page writes is the one Hermes's dashboard verifies.

    The provider lives in the Hermes checkout, not this repo, so the test skips
    on a machine without it (a fresh clone, CI) and runs on HQ and on a tenant.
    It is the only assertion in the repo that pins the *vendor's* format rather
    than the installer's copy of it; if Hermes changes scrypt parameters or the
    hash string, this is what fails before a user cannot sign in.
    """

    PROGRAM = r'''
import sys, types
sys.path.insert(0, sys.argv[1])
class _Any(types.ModuleType):
    def __getattr__(self, name):
        return type(name, (), {})
sys.modules.setdefault("httpx", _Any("httpx"))
from plugins.dashboard_auth.basic import hash_password, _verify_password
if sys.argv[2] == "hash":
    print(hash_password(sys.argv[3]))
elif sys.argv[2] == "verify":
    print("yes" if _verify_password(sys.argv[3], sys.argv[4]) else "no")
else:
    raise SystemExit(2)
'''

    def setUp(self):
        self.root = hermes_agent_root()
        if self.root is None:
            self.skipTest("the Hermes checkout is not on this machine")

    def hermes(self, *args: str) -> list[str]:
        result = subprocess.run(
            [sys.executable, "-c", self.PROGRAM, str(self.root), *args],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().splitlines()

    def test_the_guest_hash_verifies_in_hermes_and_hers_in_the_page(self):
        encoded = fv.hash_password(PASSWORD)
        self.assertEqual(self.hermes("verify", PASSWORD, encoded), ["yes"])
        self.assertEqual(self.hermes("verify", PASSWORD + "x", encoded), ["no"])
        hermes_hash = self.hermes("hash", PASSWORD)[0]
        self.assertTrue(hermes_hash.startswith("scrypt$16384$8$1$"), hermes_hash[:24])
        self.assertTrue(fv.verify_password(PASSWORD, hermes_hash))
        self.assertFalse(fv.verify_password("nope", hermes_hash))


class TokenTests(unittest.TestCase):
    """B-once (ODL-925): the page is tokenless and ``?t=`` is the stale link."""

    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def test_a_bare_get_renders_the_setup_form(self):
        status, headers, page = self.harness.call("GET", "/first-visit")
        self.assertEqual(status, 200)
        self.assertIn("Choose your sign-in", page)
        self.assertIn('method="post" action="/first-visit"', page)
        self.assertNotIn('name="t"', page)
        self.assertNotIn(self.harness.token, page)
        self.assertNotIn("{{", page)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("form-action 'self'", headers.get("Content-Security-Policy", ""))

    def test_any_query_token_is_the_stale_link(self):
        """A valid, unknown, short or empty ``?t=`` never renders the form."""
        for path in ("/first-visit?t=" + self.harness.token, "/first-visit?t=",
                     "/first-visit?t=" + "a" * 64, "/first-visit?t=short"):
            with self.subTest(path=path):
                status, _, page = self.harness.call("GET", path)
                self.assertEqual(status, 410)
                self.assertIn("This link is closed", page)
                self.assertNotIn("Choose your sign-in", page)
                self.assertNotIn(">Sign in<", page)
                self.assertNotIn('type="password"', page)
                self.assertNotIn("<form", page)

    def test_a_reload_changes_nothing(self):
        for _ in range(3):
            status, _, _ = self.harness.call("GET", "/first-visit")
            self.assertEqual(status, 200)
        self.assertIsNone(self.harness.load_state()["signin_set_at"])
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)

    def test_the_setup_write_records_the_first_login_and_opens_the_login_form(self):
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)
        state = self.harness.load_state()
        self.assertIsNotNone(state["first_login_at"])
        self.assertEqual(state["signin_set_at"], state["first_login_at"],
                         "first_login_at and signin_set_at land in one write")
        self.assertEqual(state["used_at"], state["signin_set_at"])
        # The page is a login now, and the page's only credential is the
        # password just chosen.
        status, _, page = self.harness.call("GET", "/first-visit")
        self.assertEqual(status, 200)
        self.assertIn("Sign in", page)
        self.assertNotIn('name="t"', page)
        self.assertEqual(self.harness.signin(PASSWORD)[0], 200)
        self.assertEqual(self.harness.signin(PASSWORD + "x")[0], 200)

    def test_two_setup_posts_at_once_write_one_hash(self):
        results: list[int] = []
        lock = threading.Lock()

        def post(index: int) -> None:
            status, _, _ = self.harness.set_signin("racing-long-password-%04d" % index)
            with lock:
                results.append(status)

        threads = [threading.Thread(target=post, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # One write wins; the second sees a login page and refuses the other
        # password, so the stored hash accepts exactly one of the two.
        self.assertEqual(sorted(results), [200, 200], results)
        accepted = [index for index in range(2)
                    if fv.verify_password("racing-long-password-%04d" % index,
                                          self.harness.stored_hash())]
        self.assertEqual(len(accepted), 1, accepted)


class SignInLoginTests(unittest.TestCase):
    """The B-once first visit: a login, one password, the code once."""

    def setUp(self):
        self.harness = FirstVisitHarness(seeded=True)
        self.addCleanup(self.harness.close)

    def test_the_page_asks_for_one_password_and_titles_itself_sign_in(self):
        status, _, page = self.harness.call("GET", "/first-visit")
        self.assertEqual(status, 200)
        self.assertIn("<title>Sign in — Odile</title>", page)
        self.assertEqual(page.count('type="password"'), 1)
        self.assertNotIn('name="password2"', page)
        self.assertNotIn('name="t"', page)
        self.assertIn("The first sign-in shows your recovery code once and offers "
                      "your recovery file.", page)
        # D-1 verbatim, and neither retiring sentence survives.
        self.assertIn("Your Odile password opens your machine and your account "
                      "page. You chose it when you registered. It is the only "
                      "app-layer gate: no 2FA in alpha.0.", page)
        self.assertNotIn("set on your machine at first visit", page)
        self.assertNotIn("We never see it", page)

    def test_the_sign_in_page_offers_no_replace_link_before_a_code_exists(self):
        """Designer round 2: the login must not promise a code that does not exist.

        Before the first sign-in the state's ``recovery_hash`` is null and
        ``/first-visit/replace`` answers 409 "This link is closed.", so the
        sign-in page carries no replace foot and no replace link. The
        ``signed-in`` state is the one that names the action.
        """
        self.assertIsNone(self.harness.load_state()["recovery_hash"])
        status, _, page = self.harness.call("GET", "/first-visit")
        self.assertEqual(status, 200)
        self.assertNotIn("/first-visit/replace", page)
        self.assertNotIn("Replace it", page)
        self.assertNotIn("unchanged after the first visit", page)

    def test_a_wrong_password_re_renders_one_error_and_writes_nothing(self):
        before = self.harness.env_text()
        status, _, page = self.harness.signin(PASSWORD + "x")
        self.assertEqual(status, 200)
        self.assertIn("That password does not match this machine&#x27;s sign-in.", page)
        self.assertEqual(page.count('aria-invalid="true"'), 1)
        self.assertEqual(page.count('id="error"'), 1)
        self.assertEqual(self.harness.env_text(), before)
        self.assertIsNone(self.harness.load_state()["first_login_at"])
        self.assertIsNone(self.harness.load_state()["recovery_hash"])

    def test_a_wrong_password_is_rate_limited(self):
        last, headers, body = 0, {}, ""
        for _ in range(11):
            last, headers, body = self.harness.signin(PASSWORD + "x")
        self.assertEqual(last, 429, body)
        # The limit is the page, not a bare string: the sign-in block, the
        # 429, and one form-level notice above the button (brand round).
        self.assertTrue(headers.get("Content-Type", "").startswith("text/html"))
        self.assertIn("<title>Sign in — Odile</title>", body)
        self.assertEqual(body.count('role="alert"'), 1)
        self.assertIn('<p class="error" id="notice" role="alert">Too many attempts. '
                      "Try again in ten minutes.</p>", body)
        self.assertEqual(body.count('type="password"'), 1)
        self.assertIn('action="/first-visit"', body)

    def test_the_signin_field_points_at_no_missing_hint(self):
        # The sign-in block renders no #password-hint, so its one control must
        # not name one; with an error it points at #error only (brand round).
        status, _, page = self.harness.call("GET", "/first-visit")
        self.assertEqual(status, 200)
        self.assertNotIn("password-hint", page)
        self.assertNotIn("aria-describedby", page)
        status, _, page = self.harness.signin(PASSWORD + "x")
        self.assertEqual(status, 200)
        self.assertIn('aria-invalid="true" aria-describedby="error"', page)
        self.assertNotIn("password-hint", page)

    def test_the_right_password_shows_the_code_and_writes_one_state(self):
        status, headers, page = self.harness.signin()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        match = CODE_RE.search(page)
        self.assertIsNotNone(match, "the done page does not show a recovery code")
        code = match.group(1)
        self.assertEqual(len(code.replace("-", "")), 20, "the code is from the 20-character alphabet")
        self.assertIn("I have saved the code and the file", page)
        self.assertIn("Open your workspace", page)
        self.assertIn(D6_DECIDED, page)
        state = self.harness.load_state()
        self.assertIsNotNone(state["first_login_at"])
        self.assertEqual(state["signin_set_at"], self.harness.load_state()["signin_set_at"])
        self.assertIsNone(state["used_at"], "a login does not spend the setup token")
        self.assertTrue(fv.verify_password(fv.normalise_code(code),
                                           state["recovery_hash"]))
        # The .env is untouched: the password was already its verifier.
        self.assertTrue(fv.verify_password(PASSWORD, self.harness.stored_hash()))
        # The first mint requests the push once; the timer is the retry.
        self.assertEqual(self.harness.push_requests(), 1,
                         "the first code did not ask for the HQ push")
        # A later sign-in keeps the code and shows no code again (ODL-926).
        first_login = state["first_login_at"]
        status, _, again = self.harness.signin()
        self.assertEqual(status, 200)
        self.assertIn("You are signed in.", again)
        self.assertIn(D20_SUBSET, again)
        self.assertEqual(CODE_RE.search(again), None,
                         "a later sign-in showed the recovery code again")
        now = self.harness.load_state()
        self.assertEqual(now["recovery_hash"], state["recovery_hash"],
                         "a later sign-in rotated the recovery code")
        self.assertEqual(now["first_login_at"], first_login)
        self.assertEqual(self.harness.push_requests(), 1,
                         "a later sign-in asked for a push")

    def test_a_stale_token_on_a_seeded_machine_never_renders_or_writes(self):
        before = self.harness.env_text()
        for path in ("/first-visit?t=" + self.harness.token, "/first-visit?t=",
                     "/first-visit?t=" + "f" * 64):
            with self.subTest(path=path):
                status, _, page = self.harness.call("GET", path)
                self.assertEqual(status, 410)
                self.assertNotIn("<form", page)
        self.assertEqual(self.harness.env_text(), before)
        self.assertIsNone(self.harness.load_state()["first_login_at"])

    def test_a_stale_token_on_the_seeded_state_answers_already_set_with_one_way_in(self):
        # The seeded state this card creates: the .env holds the registration
        # verifier and signin_set_at is stamped, but no recovery code exists
        # until the sign-in. The stale link is "already set" — and its one
        # action is the tokenless page that signs in, never the reset route
        # (which answers 409 on this state).
        status, _, page = self.harness.call("GET", "/first-visit?t=" + self.harness.token)
        self.assertEqual(status, 410)
        self.assertIn("The sign-in is already set, or the link is not valid.", page)
        self.assertNotIn("<form", page)
        self.assertNotIn("/first-visit/reset", page)
        self.assertEqual(
            re.findall(r'<a\b[^>]*href="(/[^"]*)"', page), ["/first-visit"])
        self.assertIn('<a class="primary" href="/first-visit">Sign in</a>', page)


class RefusedPageTests(unittest.TestCase):
    """The stale link and the closed reset route (ODL-925, ODL-471 follow-up).

    ``?t=`` is the A-era link: any value, on any machine, answers 410 and
    renders no form. A machine with no sign-in yet offers the tokenless page
    (its setup form); once a sign-in and a code exist, the reset form is the
    way back.
    """

    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def test_the_stale_link_before_a_signin_offers_the_tokenless_page(self):
        for path in ("/first-visit?t=" + self.harness.token, "/first-visit?t=short",
                     "/first-visit?t="):
            with self.subTest(path=path):
                status, _, page = self.harness.call("GET", path)
                self.assertEqual(status, 410)
                self.assertIn("This link is closed", page)
                self.assertNotIn("/first-visit/reset", page)
                self.assertNotIn("Reset with a recovery code", page)
                # The one action is the tokenless page, which on this machine
                # is the setup form; nothing left to click can refuse.
                self.assertEqual(
                    re.findall(r'<a\b[^>]*href="(/[^"]*)"', page), ["/first-visit"])
                self.assertIn("Open the first-visit page", page)
                self.assertIn("no sign-in has been set on this machine yet", page)
        # The route the page does not offer still refuses; that is why it is gone.
        self.assertEqual(self.harness.call("GET", "/first-visit/reset")[0], 409)

    def test_the_stale_link_after_a_code_still_offers_the_reset_form(self):
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)
        status, _, page = self.harness.call("GET", "/first-visit?t=" + self.harness.token)
        self.assertEqual(status, 410)
        self.assertIn('href="/first-visit/reset"', page)
        self.assertIn("Reset with a recovery code", page)
        status, _, form = self.harness.call("GET", "/first-visit/reset")
        self.assertEqual(status, 200)
        self.assertIn('name="code"', form)
        self.assertIn("Reset your sign-in", form)

    def test_an_unknown_token_on_a_machine_with_a_code_keeps_the_reset_link(self):
        # The sign-in is set, so a recovery code exists and the reset route
        # answers 200 even though this token matches nothing.
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)
        status, _, page = self.harness.call("GET", "/first-visit?t=" + "b" * 64)
        self.assertEqual(status, 410)
        self.assertIn('href="/first-visit/reset"', page)
        self.assertEqual(self.harness.call("GET", "/first-visit/reset")[0], 200)

    def test_the_closed_reset_route_offers_no_link_to_itself(self):
        status, _, page = self.harness.call("GET", "/first-visit/reset")
        self.assertEqual(status, 409)
        self.assertIn("This link is closed", page)
        self.assertNotIn('href="/first-visit/reset"', page)


class SignInTests(unittest.TestCase):
    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def test_a_short_password_is_refused_and_the_token_stays_unspent(self):
        status, _, page = self.harness.set_signin("short")
        self.assertEqual(status, 200)
        self.assertIn("Use at least 16 characters.", page)
        self.assertEqual(self.harness.load_state()["used_at"], None)
        self.assertIn("scrypt$16384$8$1$AAAA$BBBB", self.harness.env_text())
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)

    def test_a_mismatch_is_refused(self):
        status, _, page = self.harness.call(
            "POST", "/first-visit",
            {"t": self.harness.token, "password": PASSWORD, "password2": PASSWORD + "x"})
        self.assertEqual(status, 200)
        self.assertIn("The two passwords do not match.", page)
        self.assertIsNone(self.harness.load_state()["used_at"])

    def test_a_short_password_puts_the_message_under_its_field_only(self):
        status, _, page = self.harness.set_signin("short")
        self.assertEqual(status, 200)
        self.assertIn("Use at least 16 characters.", page)
        self.assertNotIn('class="err"', page, "the message is not a card")
        self.assertEqual(page.count('aria-invalid="true"'), 1)
        self.assertIn('aria-invalid="true" aria-describedby="password-hint error"', page)
        # The message sits inside the password field, above the repeat field.
        self.assertGreater(page.index('id="error"'), page.index('name="password"'))
        self.assertLess(page.index('id="error"'), page.index('for="password2"'))

    def test_a_mismatch_marks_both_password_fields_and_nothing_else(self):
        status, _, page = self.harness.call(
            "POST", "/first-visit",
            {"t": self.harness.token, "password": PASSWORD, "password2": PASSWORD + "x"})
        self.assertEqual(status, 200)
        self.assertIn("The two passwords do not match.", page)
        self.assertEqual(page.count('aria-invalid="true"'), 2)
        self.assertEqual(page.count('aria-describedby="password-hint error"'), 1)
        self.assertEqual(page.count('aria-describedby="error"'), 1)
        self.assertGreater(page.index('id="error"'), page.index('name="password2"'))

    def test_the_post_writes_the_hash_on_the_guest(self):
        before = self.harness.env.stat()
        status, headers, page = self.harness.set_signin()
        self.assertEqual(status, 200)
        text = self.harness.env_text()
        # The three-key allowlist survives, in order, and the plaintext never lands.
        self.assertEqual(
            [line.split("=", 1)[0] for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith("#")],
            ["HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
             "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
             "HERMES_DASHBOARD_BASIC_AUTH_SECRET"])
        self.assertIn(D1_DECIDED, PAGE.read_text(encoding="utf-8"))
        self.assertTrue(fv.verify_password(PASSWORD, self.harness.stored_hash()))
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(PASSWORD, self.harness.state.read_text(encoding="utf-8"))
        after = self.harness.env.stat()
        self.assertEqual(after.st_mode & 0o777, 0o600, "the .env must stay 0600")
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        self.assertNotIn(PASSWORD, page)
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_the_post_keeps_the_machines_secret(self):
        """ODL-522: setting the sign-in must not rotate the session secret.

        Every session cookie the owner ever holds is signed with this value, so
        a POST that regenerated it would sign the owner out of every browser on
        the machine the moment they set their password. Only the hash line
        moves.
        """
        before = [line for line in self.harness.env_text().splitlines()
                  if line.startswith(fv.SECRET_KEY + "=")]
        self.assertEqual(len(before), 1)
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)
        after = [line for line in self.harness.env_text().splitlines()
                 if line.startswith(fv.SECRET_KEY + "=")]
        self.assertEqual(after, before, "the POST rotated the machine's session secret")

    def test_the_post_refuses_an_env_with_no_secret(self):
        """A tenant with no signing key gets no password-only file.

        The first boot generates this machine's secret; a .env without one
        cannot sign a cookie, so the page refuses before it writes, leaves the
        token unspent and the file untouched, and the retry that restores the
        line succeeds. The refusal is a 500 the user can retry, not a silent
        write of a dead sign-in.
        """
        without = "\n".join(
            line for line in self.harness.env_text().splitlines()
            if not line.startswith(fv.SECRET_KEY + "=")) + "\n"
        self.harness.env.write_text(without, encoding="utf-8")
        status, _, body = self.harness.set_signin()
        self.assertEqual(status, 500)
        self.assertIn("The sign-in was not set. Try again.", body)
        self.assertIsNone(self.harness.load_state()["used_at"],
                          "a refused write must leave the token unspent")
        self.assertEqual(self.harness.env_text(), without,
                         "a refused write must leave the .env exactly as it was")
        # The token is still good: restoring the line and retrying works.
        self.harness.env.write_text(tenant_env(), encoding="utf-8")
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)
        self.assertTrue(fv.verify_password(PASSWORD, self.harness.stored_hash()))

    def test_the_recovery_code_is_shown_once_and_stored_hashed(self):
        status, _, page = self.harness.set_signin()
        self.assertEqual(status, 200)
        match = CODE_RE.search(page)
        self.assertIsNotNone(match, "the done page does not show a recovery code")
        code = match.group(1)
        self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{4}(-[A-HJ-NP-Z2-9]{4}){4}$")
        state_text = self.harness.state.read_text(encoding="utf-8")
        self.assertNotIn(code, state_text, "the code must not be stored in the clear")
        self.assertNotIn(fv.normalise_code(code), state_text)
        state = self.harness.load_state()
        self.assertTrue(state["recovery_hash"].startswith("scrypt$"))
        self.assertTrue(fv.verify_password(fv.normalise_code(code), state["recovery_hash"]))
        # Re-fetching the page cannot show it again: the page is the login now.
        status, _, page = self.harness.call("GET", "/first-visit")
        self.assertEqual(status, 200)
        self.assertNotIn(code, page)

    def test_the_reload_runs_after_the_hash_is_written(self):
        self.assertEqual(self.harness.call("GET", "/health")[0], 200)
        self.assertFalse(self.harness.reload_log.is_file())
        self.harness.set_signin()
        self.assertTrue(self.harness.reload_log.is_file(),
                        "the service must reload the agent or the hash is inert")
        self.assertEqual(self.harness.call("GET", "/health")[0], 200)


class ReloadFailureTests(unittest.TestCase):
    def test_a_failed_reload_fails_loudly_and_keeps_the_token(self):
        harness = None
        try:
            harness = FirstVisitHarness(reload_command="false")
            status, _, body = harness.set_signin()
            self.assertEqual(status, 500)
            self.assertIn("Try again", body)
            self.assertIsNone(harness.load_state()["used_at"],
                              "a failed reload must leave the token unspent")
            status, _, _ = harness.set_signin()   # a retry is still possible
            self.assertEqual(status, 500)
        finally:
            if harness is not None:
                harness.close()


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def sign_in_and_read_code(self) -> str:
        status, _, page = self.harness.set_signin()
        self.assertEqual(status, 200)
        match = CODE_RE.search(page)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_the_reset_rotates_the_session_secret(self):
        """ODL-579: a reset ends the machine's other sessions.

        ``write_env_hash`` rotates HERMES_DASHBOARD_BASIC_AUTH_SECRET in the
        same write as the hash, so every cookie signed with the old value dies.
        A session token minted by the vendored provider before the reset is
        refused afterwards; the pre-reset provider is the control.
        """
        code = self.sign_in_and_read_code()
        before = fv.env_values(self.harness.env_text())[fv.SECRET_KEY]
        old_hash = self.harness.stored_hash()
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        after = fv.env_values(self.harness.env_text())[fv.SECRET_KEY]
        self.assertNotEqual(after, before, "the reset kept the machine's session secret")
        self.assertEqual(len(base64.b64decode(after)), 32)
        self.assertEqual(self.harness.env.stat().st_mode & 0o777, 0o600)
        new_hash = self.harness.stored_hash()
        self.assertNotEqual(new_hash, old_hash)
        self.assertNotIn(before, self.harness.env_text())
        self.assertNotIn(PASSWORD, self.harness.env_text())
        self.assertNotIn(NEW_PASSWORD, self.harness.env_text())

    def test_the_reset_form_is_closed_before_the_sign_in_is_set(self):
        status, _, page = self.harness.call("GET", "/first-visit/reset")
        self.assertEqual(status, 409)
        self.assertIn("This link is closed", page)

    def test_a_wrong_code_is_refused_and_the_hash_is_kept(self):
        self.sign_in_and_read_code()
        before = self.harness.stored_hash()
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": "AAAA-BBBB-CCCC-DDDD-EEEE", "password": NEW_PASSWORD,
             "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn("That recovery code does not match.", page)
        self.assertEqual(self.harness.stored_hash(), before)

    def test_a_wrong_code_marks_only_the_code_field(self):
        self.sign_in_and_read_code()
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": "AAAA-BBBB-CCCC-DDDD-EEEE", "password": NEW_PASSWORD,
             "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(page.count('aria-invalid="true"'), 1)
        self.assertIn('aria-describedby="error"', page)
        self.assertGreater(page.index('id="error"'), page.index('name="code"'))
        self.assertLess(page.index('id="error"'), page.index('id="password"'))

    def test_a_valid_code_survives_a_short_password(self):
        code = self.sign_in_and_read_code()
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": "short", "password2": "short"})
        self.assertEqual(status, 200)
        self.assertIn("Use at least 16 characters.", page)
        self.assertIn('value="%s"' % code, page, "the submitted code is not kept")
        self.assertEqual(page.count('aria-invalid="true"'), 1)
        self.assertIn('aria-describedby="password-hint error"', page)

    def test_a_valid_code_survives_a_password_mismatch(self):
        code = self.sign_in_and_read_code()
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD + "x"})
        self.assertEqual(status, 200)
        self.assertIn("The two passwords do not match.", page)
        self.assertIn('value="%s"' % code, page, "the submitted code is not kept")
        self.assertEqual(page.count('aria-invalid="true"'), 2)
        self.assertGreater(page.index('id="error"'), page.index('name="password2"'))

    def test_a_short_new_password_is_refused_after_a_good_code(self):
        code = self.sign_in_and_read_code()
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": "short", "password2": "short"})
        self.assertEqual(status, 200)
        self.assertIn("Use at least 16 characters.", page)
        self.assertTrue(fv.verify_password(PASSWORD, self.harness.stored_hash()))

    def test_the_reset_works_twice_with_the_same_code_and_shows_no_new_code(self):
        """ODL-926: the code is stable, so the same code resets twice."""
        code = self.sign_in_and_read_code()
        status, headers, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(CODE_RE.search(page), None,
                         "the reset showed a new recovery code")
        self.assertIn("Your sign-in is changed.", page)
        self.assertIn(D20_SUBSET, page)
        self.assertTrue(fv.verify_password(NEW_PASSWORD, self.harness.stored_hash()))
        self.assertFalse(fv.verify_password(PASSWORD, self.harness.stored_hash()))
        state = self.harness.load_state()
        self.assertTrue(fv.verify_password(fv.normalise_code(code), state["recovery_hash"]),
                        "the reset rotated the recovery code")
        self.assertIsNone(state["recovery_used_at"],
                          "the reset wrote a spend marker that is no longer read")
        self.assertNotIn(NEW_PASSWORD, self.harness.env_text())
        # The same code resets a second time, and the reset page says why.
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": "third-long-password-0003",
             "password2": "third-long-password-0003"})
        self.assertEqual(status, 200)
        self.assertIn("Your sign-in is changed.", page)
        self.assertNotIn("That recovery code does not match.", page)
        self.assertTrue(fv.verify_password("third-long-password-0003",
                                           self.harness.stored_hash()))
        status, _, form = self.harness.call("GET", "/first-visit/reset")
        self.assertEqual(status, 200)
        self.assertIn(D20_DECIDED, form, "the reset page does not carry D-20 verbatim")
        # A wrong code is still refused.
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": "AAAA-BBBB-CCCC-DDDD-EEEE", "password": NEW_PASSWORD,
             "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn("That recovery code does not match.", page)

    def test_a_pasted_code_with_spaces_and_lowercase_works(self):
        code = self.sign_in_and_read_code()
        pasted = " " + code.lower().replace("-", " ") + " "
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": pasted, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(fv.verify_password(NEW_PASSWORD, self.harness.stored_hash()))


class DonePageHeadingTests(unittest.TestCase):
    """The done page's h1 and title name the action the visit completed.

    ODL-947 follow-up: on a seeded first boot the sign-in was chosen at
    registration, so the visit signed in; only a classic setup sets one. The
    code, the file and the state are the same in both, so only the two
    sentences may move.
    """

    def page(self, *, seeded: bool) -> str:
        harness = FirstVisitHarness(seeded=seeded)
        self.addCleanup(harness.close)
        status, _, page = (harness.signin() if seeded else harness.set_signin())
        self.assertEqual(status, 200)
        return page

    def test_a_seeded_login_says_it_signed_in(self):
        page = self.page(seeded=True)
        self.assertIn("<title>You are signed in \u2014 Odile</title>", page)
        self.assertIn("<h1>You are signed in.</h1>", page)
        self.assertNotIn("Your sign-in is set", page)
        self.assertIn("This is the only time the code is shown.", page)
        self.assertIsNotNone(CODE_RE.search(page))

    def test_a_classic_setup_says_it_set_the_sign_in(self):
        page = self.page(seeded=False)
        self.assertIn("<title>Your sign-in is set \u2014 Odile</title>", page)
        self.assertIn("<h1>Your sign-in is set.</h1>", page)
        self.assertNotIn("You are signed in", page)
        self.assertIn("This is the only time the code is shown.", page)
        self.assertIsNotNone(CODE_RE.search(page))

    def test_the_two_pages_differ_only_in_the_title_and_the_heading(self):
        pages = {seeded: self.page(seeded=seeded) for seeded in (True, False)}

        def normalise(page: str) -> str:
            page = re.sub(r"<title>[^<]*</title>", "<title>T</title>", page, count=1)
            page = re.sub(r"<h1>[^<]*</h1>", "<h1>H</h1>", page, count=1)
            page = re.sub(r'id="code">[^<]*</p>', 'id="code">CODE</p>', page, count=1)
            return re.sub(r"\?d=[0-9a-f]{64}", "?d=TOKEN", page)

        self.assertEqual(normalise(pages[True]), normalise(pages[False]),
                         "the two done pages differ outside the title and the h1")


class RecoveryDownloadTests(unittest.TestCase):
    """The recovery file is a download beside the code (ODL-471)."""

    def harness(self, **kwargs) -> FirstVisitHarness:
        harness = FirstVisitHarness(**kwargs)
        self.addCleanup(harness.close)
        return harness

    def signed_in(self, harness: FirstVisitHarness,
                  headers: dict[str, str] | None = None) -> tuple[str, str]:
        status, _, page = harness.set_signin(headers=headers)
        self.assertEqual(status, 200)
        match = CODE_RE.search(page)
        assert match is not None, "the done page has no recovery code"
        return match.group(1), page

    def test_a_missing_recovery_file_leaves_the_download_off_and_names_it(self):
        harness = self.harness()
        code, page = self.signed_in(harness)
        self.assertNotIn("/first-visit/recovery", page)
        self.assertIn("The recovery file is not available on this machine.", page)
        status, headers, body = harness.download("0" * 64)
        self.assertEqual(status, 404)
        self.assertIn("text/plain", headers.get("Content-Type", ""))
        self.assertTrue(body.strip(), "a 404 must still carry a body")

    def test_an_empty_recovery_file_is_unavailable_never_an_empty_download(self):
        harness = self.harness(recovery="")
        code, page = self.signed_in(harness)
        self.assertNotIn('class="download"', page)
        self.assertIn("The recovery file is not available on this machine.", page)
        status, _, body = harness.download("0" * 64)
        self.assertEqual(status, 404)
        self.assertTrue(body.strip())

    def test_the_done_page_offers_the_file_beside_the_recovery_code(self):
        harness = self.harness(recovery=recovery_text())
        code, page = self.signed_in(harness)
        token = harness.download_token(page)
        self.assertIn('class="download"', page)
        self.assertIn('href="/first-visit/recovery?d=%s"' % token, page)
        self.assertNotIn(code, page.split('class="download"')[1].split(">")[0],
                         "the code is never a bearer in the download URL")
        self.assertIn('download="odile-recovery-odl-test.txt"', page)
        self.assertLess(page.index('id="code"'), page.index('class="download"'))
        self.assertIn(D6_DECIDED, page)

    def test_the_done_page_fills_the_download_not_the_workspace_link(self):
        # The card's hierarchy finding: the download must not skip, so it is
        # the one filled control; the workspace link is quiet. Both done
        # states carry a code and a download, and the warning sits above it.
        harness = self.harness(recovery=recovery_text())
        code, done = self.signed_in(harness)
        status, _, reset_done = harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        replaced = harness.replace(NEW_PASSWORD)[2]
        for number, (page, warning) in enumerate((
                (done, "This is the only time the code is shown."),
                (replaced, "This is the only time the new code is shown."))):
            with self.subTest(state=number):
                self.assertIn('class="download"', page)
                self.assertNotIn('class="primary"', page,
                                 "the download is the done page's one filled control")
                self.assertLess(page.index(warning), page.index('class="download"'),
                                "the warning sits above the download")
        # The reset's file-only page keeps the download and shows no code.
        self.assertIn('class="download"', reset_done)
        self.assertEqual(CODE_RE.search(reset_done), None)
        refused = harness.call("GET", "/first-visit?t=" + "a" * 64)[2]
        self.assertEqual(refused.count('class="primary"'), 1,
                         "the refused state keeps its one filled link")

    def test_the_download_is_the_recovery_file_text_plain_with_a_filename(self):
        harness = self.harness(recovery=recovery_text())
        code, page = self.signed_in(harness)
        token = harness.download_token(page)
        before = harness.recovery.stat()
        status, headers, body = harness.download(token)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/plain; charset=utf-8")
        self.assertEqual(headers.get("Content-Disposition"),
                         'attachment; filename="odile-recovery-odl-test.txt"')
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn(BACKUP_PASSWORD, body)
        self.assertEqual(body, harness.recovery.read_text(encoding="utf-8"))
        after = harness.recovery.stat()
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))

    def test_a_served_certificate_fills_the_fingerprint_and_writes_it_back(self):
        with tempfile.TemporaryDirectory(prefix="odl471-store-") as tmp:
            store = Path(tmp)
            store, expected = certificate_fixture(store, "odl-test.example")
            # The same digest as the certificate the TLS socket would serve.
            pem = (store / "acme" / "odl-test.example" / "odl-test.example.crt").read_text(
                encoding="utf-8")
            self.assertEqual(expected, "sha256:" + hashlib.sha256(
                fv.leaf_der(pem)).hexdigest())

            harness = self.harness(recovery=recovery_text(), caddy_stores=[store])
            headers = {"Host": "odl-test.example", "X-Forwarded-Proto": "https"}
            code, page = self.signed_in(harness, headers=headers)
            token = harness.download_token(page)
            self.assertNotIn("pending on this machine", page)
            on_disk = harness.recovery.read_text(encoding="utf-8")
            self.assertIn("Certificate SHA-256: " + expected, on_disk)
            self.assertNotIn(PLACEHOLDER, on_disk)
            written = harness.recovery.stat().st_mtime_ns

            status, _, body = harness.download(token, headers=headers)
            self.assertEqual(status, 200)
            self.assertIn(expected, body)
            self.assertEqual(body, on_disk)
            self.assertEqual(harness.recovery.stat().st_mtime_ns, written,
                             "a filled file is not rewritten on every download")

    def test_a_bench_without_a_certificate_keeps_the_placeholder_and_says_so(self):
        with tempfile.TemporaryDirectory(prefix="odl471-empty-") as tmp:
            harness = self.harness(recovery=recovery_text(), caddy_stores=[Path(tmp)])
            headers = {"Host": "odl-test.example", "X-Forwarded-Proto": "https"}
            code, page = self.signed_in(harness, headers=headers)
            token = harness.download_token(page)
            self.assertIn("certificate fingerprint is pending on this machine", page)
            self.assertIn(PLACEHOLDER, harness.recovery.read_text(encoding="utf-8"))
            status, _, body = harness.download(token, headers=headers)
            self.assertEqual(status, 200)
            self.assertIn(PLACEHOLDER, body)

    def test_a_chain_file_hashes_the_leaf_the_browser_shows(self):
        """certmagic writes leaf + issuer into one .crt; only the leaf is pinned."""
        with tempfile.TemporaryDirectory(prefix="odl471-chain-") as tmp:
            store = Path(tmp)
            store, leaf, chained = certificate_chain_fixture(store, "odl-test.example")
            self.assertNotEqual(leaf, chained, "the fixture must reproduce the bug")
            bundle = (store / "local" / "odl-test.example" /
                      "odl-test.example.crt").read_text(encoding="utf-8")
            self.assertEqual(bundle.count("-----BEGIN CERTIFICATE-----"), 2,
                             "the store file must carry the leaf and its issuer")
            self.assertEqual(leaf, "sha256:" + hashlib.sha256(
                fv.leaf_der(bundle)).hexdigest())

            harness = self.harness(recovery=recovery_text(), caddy_stores=[store])
            headers = {"Host": "odl-test.example", "X-Forwarded-Proto": "https"}
            code, page = self.signed_in(harness, headers=headers)
            token = harness.download_token(page)
            self.assertNotIn("pending on this machine", page,
                             "a chain file is a certificate; the page says so")
            on_disk = harness.recovery.read_text(encoding="utf-8")
            self.assertIn("Certificate SHA-256: " + leaf, on_disk)
            self.assertNotIn(chained, on_disk)
            status, _, body = harness.download(token, headers=headers)
            self.assertEqual(status, 200)
            self.assertIn(leaf, body)
            self.assertNotIn(chained, body)

    def test_a_malformed_store_file_is_skipped_for_a_valid_one(self):
        with tempfile.TemporaryDirectory(prefix="odl471-junk-") as tmp:
            store = Path(tmp)
            store, leaf, _chained = certificate_chain_fixture(store, "odl-test.example")
            junk = store / "acme" / "odl-test.example"
            junk.mkdir(parents=True)
            broken = junk / "odl-test.example.crt"
            broken.write_text(
                "-----BEGIN CERTIFICATE-----\nnot base64 at all\n"
                "-----END CERTIFICATE-----\n", encoding="utf-8")
            # Newest wins only among parseable files; junk must not shadow one.
            os.utime(broken, None)
            harness = self.harness(recovery=recovery_text(), caddy_stores=[store])
            headers = {"Host": "odl-test.example", "X-Forwarded-Proto": "https"}
            _code, page = self.signed_in(harness, headers=headers)
            self.assertNotIn("pending on this machine", page)
            self.assertIn("Certificate SHA-256: " + leaf,
                          harness.recovery.read_text(encoding="utf-8"))

    def test_a_store_with_only_a_malformed_file_keeps_the_placeholder(self):
        with tempfile.TemporaryDirectory(prefix="odl471-onlyjunk-") as tmp:
            store = Path(tmp)
            junk = store / "acme" / "odl-test.example"
            junk.mkdir(parents=True)
            (junk / "odl-test.example.crt").write_text("not a certificate\n",
                                                        encoding="utf-8")
            harness = self.harness(recovery=recovery_text(), caddy_stores=[store])
            headers = {"Host": "odl-test.example", "X-Forwarded-Proto": "https"}
            _code, page = self.signed_in(harness, headers=headers)
            self.assertIn("certificate fingerprint is pending on this machine", page)
            self.assertIn(PLACEHOLDER, harness.recovery.read_text(encoding="utf-8"))

    def test_a_plain_http_request_keeps_the_placeholder_even_with_a_certificate(self):
        with tempfile.TemporaryDirectory(prefix="odl471-http-") as tmp:
            store = Path(tmp)
            store, expected = certificate_fixture(store, "odl-test.example")
            harness = self.harness(recovery=recovery_text(), caddy_stores=[store])
            code, page = self.signed_in(harness, headers={"Host": "odl-test.example"})
            self.assertNotIn(expected, page)
            self.assertIn(PLACEHOLDER, harness.recovery.read_text(encoding="utf-8"))

    def test_the_download_requires_a_live_one_time_token(self):
        harness = self.harness(recovery=recovery_text())
        code, page = self.signed_in(harness)
        token = harness.download_token(page)
        for wrong in ("0" * 64, "", "short", code):
            with self.subTest(wrong=wrong):
                status, _, body = harness.download(wrong)
                self.assertEqual(status, 404)
                self.assertNotIn(BACKUP_PASSWORD, body)
        status, _, body = harness.download(token)
        self.assertEqual(status, 200)
        self.assertIn(BACKUP_PASSWORD, body)

    def test_the_first_visits_download_does_not_spend_the_code(self):
        """The done page's button is a download token; the code stays live."""
        harness = self.harness(recovery=recovery_text())
        code, page = self.signed_in(harness)
        self.assertEqual(harness.download(harness.download_token(page))[0], 200)
        state = harness.load_state()
        self.assertIsNone(state["recovery_used_at"],
                          "the first visit's download spent the recovery code")
        self.assertTrue(fv.verify_password(PASSWORD, harness.stored_hash()))
        status, _, page = harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(fv.verify_password(NEW_PASSWORD, harness.stored_hash()))

    def test_a_download_token_is_single_use_and_ten_minutes(self):
        harness = self.harness(recovery=recovery_text())
        _code, page = self.signed_in(harness)
        token = harness.download_token(page)
        state = harness.load_state()
        self.assertEqual(len(token), 64)
        self.assertEqual(state["download_sha256"], fv.hash_token(token))
        self.assertNotIn(token, json.dumps(state))
        self.assertEqual(len(state["download_expires_at"]), 20)
        self.assertEqual(harness.download(token)[0], 200)
        status, _, body = harness.download(token)
        self.assertEqual(status, 410)
        self.assertNotIn(BACKUP_PASSWORD, body, "a spent token served the file twice")
        # A refresh of the done page mints no second token: the page is closed.
        self.assertEqual(harness.call(
            "GET", "/first-visit?t=" + harness.token)[0], 410)

    def test_a_head_on_the_download_does_not_spend_the_token(self):
        """A link previewer's HEAD must not burn the one use (ODL-579)."""
        harness = self.harness(recovery=recovery_text())
        _code, page = self.signed_in(harness)
        token = harness.download_token(page)
        connection = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=20)
        try:
            connection.request("HEAD", "/first-visit/recovery?d=" + token)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
        finally:
            connection.close()
        self.assertIsNone(harness.load_state()["download_used_at"],
                          "HEAD spent the one-time download token")
        status, _, body = harness.download(token)
        self.assertEqual(status, 200)
        self.assertIn(BACKUP_PASSWORD, body)

    def test_an_expired_download_token_mints_nothing_and_serves_nothing(self):
        harness = self.harness(recovery=recovery_text())
        _code, page = self.signed_in(harness)
        token = harness.download_token(page)
        state = harness.load_state()
        state["download_expires_at"] = "2026-01-01T00:00:00Z"
        harness.state.write_text(json.dumps(state), encoding="utf-8")
        status, _, body = harness.download(token)
        self.assertEqual(status, 410)
        self.assertNotIn(BACKUP_PASSWORD, body)

    def test_the_reset_done_page_offers_the_recovery_file_and_no_code(self):
        harness = self.harness(recovery=recovery_text())
        code, first = self.signed_in(harness)
        first_token = harness.download_token(first)
        status, _, page = harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(CODE_RE.search(page), None,
                         "the reset done page showed a recovery code")
        self.assertIn(D20_SUBSET, page)
        self.assertNotIn("I have saved the code and the file", page)
        token = harness.download_token(page)
        self.assertNotEqual(token, first_token, "the reset reused the first visit's token")
        self.assertEqual(harness.download(first_token)[0], 404,
                         "the reset kills the first visit's download token")
        status, _, body = harness.download(token)
        self.assertEqual(status, 200)
        self.assertIn(BACKUP_PASSWORD, body)

    def test_the_recovery_page_use_neither_spends_nor_rotates_the_code(self):
        harness = self.harness(recovery=recovery_text())
        code, _page = self.signed_in(harness)
        before = harness.load_state()["recovery_hash"]
        status, headers, page = harness.use_code(code)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(CODE_RE.search(page), None,
                         "a recovery-page use showed a new recovery code")
        self.assertIn(D20_SUBSET, page)
        token = harness.download_token(page)
        status, _, body = harness.download(token)
        self.assertEqual(status, 200)
        self.assertIn(BACKUP_PASSWORD, body)
        state = harness.load_state()
        self.assertEqual(state["recovery_hash"], before,
                         "the recovery page rotated the code")
        # The same code still opens both sites, repeatedly (ODL-926).
        for _ in range(2):
            status, _, again = harness.use_code(code)
            self.assertEqual(status, 200)
            self.assertNotIn("That recovery code does not match.", again)
        status, _, page = harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": "third-long-password-0003",
             "password2": "third-long-password-0003"})
        self.assertEqual(status, 200)
        self.assertIn("Your sign-in is changed.", page)
        self.assertTrue(fv.verify_password("third-long-password-0003",
                                           harness.stored_hash()))

    def test_the_recovery_page_renders_a_form_and_no_code_bearer(self):
        harness = self.harness(recovery=recovery_text())
        _code, _page = self.signed_in(harness)
        status, headers, page = harness.call("GET", "/first-visit/recovery")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn('action="/first-visit/recovery"', page)
        self.assertIn('name="code"', page)
        self.assertNotIn("?c=", page)
        self.assertNotIn("?d=", page)

    def test_the_legacy_spent_marker_no_longer_refuses_the_code(self):
        """ODL-926: ``recovery_used_at`` is a retired marker, not a gate.

        An older state that carries one (ODL-579 wrote it) still accepts its
        code at both sites; a state whose marker is set by hand proves the
        marker is never read.
        """
        harness = self.harness(recovery=recovery_text())
        code, _page = self.signed_in(harness)
        state = harness.load_state()
        state["recovery_used_at"] = state["recovery_issued_at"]
        harness.state.write_text(json.dumps(state), encoding="utf-8")
        self.assertTrue(fv.verify_password(fv.normalise_code(code), state["recovery_hash"]))
        status, _, page = harness.use_code(code)
        self.assertEqual(status, 200)
        self.assertNotIn("That recovery code does not match.", page)
        status, _, page = harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertNotIn("That recovery code does not match.", page)
        self.assertIn("Your sign-in is changed.", page)


class ConnectionTests(unittest.TestCase):
    """RFC 9112 §9.3: a refusal must not leave an unread body in the stream."""

    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def connection(self):
        return http.client.HTTPConnection("127.0.0.1", self.harness.port, timeout=20)

    def test_a_refused_post_drains_its_body_and_the_next_request_works(self):
        connection = self.connection()
        try:
            connection.request("POST", "/not-a-route", body=b"x" * 100,
                               headers={"Content-Type": "text/plain"})
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            response.read()
            connection.request("GET", "/health")
            second = connection.getresponse()
            self.assertEqual(second.status, 200)
            self.assertEqual(second.read(), b"ok\n")
        finally:
            connection.close()

    def test_an_oversized_body_closes_the_connection(self):
        connection = self.connection()
        try:
            connection.request("POST", "/first-visit", body=b"x" * (fv.MAX_BODY + 1),
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            self.assertEqual(response.getheader("Connection"), "close")
            response.read()
        finally:
            connection.close()

    def test_a_head_request_has_headers_and_no_body(self):
        connection = self.connection()
        try:
            connection.request("HEAD", "/first-visit")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertGreater(int(response.getheader("Content-Length", "0")), 1000)
            self.assertEqual(response.read(), b"")
        finally:
            connection.close()

    def test_a_missing_state_file_answers_500_instead_of_dropping_the_connection(self):
        # load_state raises SystemExit for the CLI; in a handler thread that
        # would end the request with no answer at all.
        self.harness.state.unlink()
        status, _, body = self.harness.call("GET", "/first-visit?t=" + self.harness.token)
        self.assertEqual(status, 500)
        self.assertIn("not configured", body)
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 500)


class PageTests(unittest.TestCase):
    """The surface: no JavaScript, no third-party request, 390 px ready."""

    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def test_every_state_is_no_js_and_makes_no_third_party_request(self):
        pages = [
            self.harness.call("GET", "/first-visit?t=" + self.harness.token)[2],
            self.harness.call("GET", "/first-visit?t=" + "a" * 64)[2],
            self.harness.call("POST", "/first-visit",
                              {"t": self.harness.token, "password": "short",
                               "password2": "short"})[2],
            self.harness.call("GET", "/first-visit/reset")[2],
            self.harness.call("GET", "/first-visit/replace")[2],
        ]
        status, _, done = self.harness.set_signin()
        self.assertEqual(status, 200)
        pages.append(done)
        code = CODE_RE.search(done).group(1)
        pages.append(self.harness.call("GET", "/first-visit/replace")[2])
        pages.append(self.harness.replace()[2])
        pages.append(self.harness.call("POST", "/first-visit/recovery",
                                       {"code": code})[2])
        status, _, reset = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        pages.append(reset)
        for number, page in enumerate(pages):
            with self.subTest(state=number):
                self.assertNotIn("<script", page.lower())
                self.assertNotIn("javascript:", page.lower())
                self.assertNotIn("onclick", page.lower())
                self.assertEqual(re.findall(r"https?://", page), [])
                self.assertIn('name="viewport" content="width=device-width', page)
                self.assertNotIn("{{", page)

    def test_the_failed_csp_and_the_narrow_rules_are_in_the_page(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("default-src 'none'", page)
        self.assertIn("@media (max-width:400px)", page)
        self.assertIn("--paper:#eadfc4", page)
        self.assertIn('autocomplete="new-password"', page)
        self.assertIn("minlength=\"16\"", page)

    def test_the_setup_form_asks_for_the_password_twice_and_hints_the_length(self):
        page = self.harness.call("GET", "/first-visit")[2]
        self.assertIn('id="password" name="password"', page)
        self.assertIn('id="password2" name="password2"', page)
        self.assertIn("At least 16 characters.", page)
        self.assertIn('aria-describedby="password-hint"', page)
        # A classic install has no registration, so its footer drops D-1's
        # middle sentence ("You chose it when you registered"); the sign-in
        # block keeps D-1 verbatim (brand round).
        self.assertNotIn("You chose it when you registered", page)
        self.assertIn("Your Odile password opens your machine and your account "
                      "page. It is the only app-layer gate: no 2FA in alpha.0.", page)

    def test_the_recovery_screens_carry_d6_and_no_coined_custody_sentence(self):
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn(D6_DECIDED, text)
        self.assertIn(D1_DECIDED, text)
        for retired in RETIRED:
            self.assertNotIn(retired, text, f"a retiring wording survives: {retired!r}")
        self.assertNotIn("We hold neither", text)
        self.assertNotIn("Only its hash stays on this machine.", text)
        self.assertIn("A7 and A9", text)
        status, _, done = self.harness.set_signin()
        self.assertEqual(status, 200)
        self.assertIn(D6_DECIDED, done)
        code = CODE_RE.search(done).group(1)
        status, _, reset_done = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn(D20_SUBSET, reset_done)
        self.assertEqual(CODE_RE.search(reset_done), None)
        replace_done = self.harness.replace(NEW_PASSWORD)[2]
        self.assertEqual(status, 200)
        self.assertIn(D6_DECIDED, replace_done)
        self.assertIn("Your new recovery code.", replace_done)

    def test_consecutive_notes_take_the_sheet_gap_and_stay_on_the_deck(self):
        """Designer round 2: D-20 gets the sheet's paragraph gap, and the
        replaced page prints no reliability sentence the deck does not carry.

        The four notes rendered at 0 px gaps because ``.note{margin:0}``; the
        adjacency rule is the sheet's 16 px paragraph gap and fires only where
        two notes meet. "Your account follows when the machine's signed note
        reaches it…" was a new public reliability sentence with no deck row, so
        it is cut.
        """
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn(".note + .note{margin-top:16px}", text)
        self.assertNotIn("Your account follows", text)
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)
        replaced = self.harness.replace()[2]
        self.assertNotIn("Your account follows", replaced)
        self.assertGreaterEqual(
            replaced.count('class="note"'), 2,
            "the replaced page no longer prints D-20 after a note")

    def test_the_reset_page_carries_the_decided_copy_and_the_stability_row(self):
        """D-14's copy, D-20 verbatim, and a no-JavaScript gate on the link."""
        status, _, page = self.harness.set_signin()
        self.assertEqual(status, 200)
        code = CODE_RE.search(page).group(1)
        page = self.harness.call("GET", "/first-visit/reset")[2]
        self.assertIn("Reset your machine's sign-in", page)
        self.assertIn("Enter the recovery code you saved at your first visit, then "
                      "choose a new sign-in.", page)
        self.assertIn("This changes the sign-in on this machine only.", page)
        # §3.4's sentence names the account host the kit and the ready email
        # carry; the machine does not know it, so the page states the split and
        # drops the fragment a stranger cannot type (designer review, round 2).
        self.assertIn("Your account password is reset at your account site.", page)
        self.assertNotIn("under /forgot", page)
        self.assertIn(D20_DECIDED, page,
                      "the reset page does not carry D-20 verbatim")
        self.assertEqual(CODE_RE.search(page), None, "the form shows no code")
        status, _, done = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn("Your sign-in is changed.", done)
        self.assertIn("Every other signed-in browser on this machine has been "
                      "signed out.", done)
        self.assertEqual(CODE_RE.search(done), None,
                         "the reset done page showed a code")
        self.assertIn(D20_SUBSET, done)
        self.assertNotIn('input type="checkbox" id="saved"', done)

    def test_the_replace_action_shows_the_new_code_once_with_the_checkbox(self):
        """ODL-926: the one rotation is the signed-in replace, on the machine."""
        status, _, first = self.harness.set_signin()
        self.assertEqual(status, 200)
        old_code = CODE_RE.search(first).group(1)
        old_hash = self.harness.load_state()["recovery_hash"]
        # The account's acknowledged hash is the old one: after the replace it
        # still is, until the push lands (the five-minute tail, §3.2 and §6).
        state = self.harness.load_state()
        state["recovery_pushed_hash"] = old_hash
        state["recovery_pushed_at"] = "2026-11-20T00:00:00Z"
        self.harness.state.write_text(json.dumps(state), encoding="utf-8")
        pushes = self.harness.push_requests()

        form = self.harness.call("GET", "/first-visit/replace")[2]
        self.assertIn("Replace your recovery code", form)
        self.assertIn('action="/first-visit/replace"', form)
        self.assertIn('name="password"', form)
        self.assertEqual(CODE_RE.search(form), None)

        status, _, wrong = self.harness.replace(PASSWORD + "x")
        self.assertEqual(status, 200)
        self.assertIn("That password does not match this machine&#x27;s sign-in.", wrong)
        self.assertEqual(wrong.count('aria-invalid="true"'), 1)
        self.assertEqual(self.harness.load_state()["recovery_hash"], old_hash,
                         "a wrong password minted a code")

        status, headers, page = self.harness.replace()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        match = CODE_RE.search(page)
        self.assertIsNotNone(match, "the replace page did not show the new code")
        new_code = match.group(1)
        self.assertNotEqual(new_code, old_code)
        self.assertRegex(new_code, r"^[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){4}$")
        self.assertIn('input type="checkbox" id="saved"', page)
        self.assertIn("I have saved the code and the file", page)
        self.assertIn(D6_DECIDED, page)
        self.assertIn(D20_DECIDED, page)
        self.assertTrue(fv.verify_password(fv.normalise_code(new_code),
                                           self.harness.load_state()["recovery_hash"]))
        self.assertFalse(fv.verify_password(fv.normalise_code(old_code),
                                            self.harness.load_state()["recovery_hash"]))
        # The old code stops working at the machine in the same transaction.
        status, _, page = self.harness.use_code(old_code)
        self.assertEqual(status, 200)
        self.assertIn("That recovery code does not match.", page)
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": old_code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn("That recovery code does not match.", page)
        # The account still holds the old hash; the new one is pending.
        state = self.harness.load_state()
        self.assertEqual(state["recovery_pushed_hash"], old_hash,
                         "the replace claimed the account already has the new hash")
        self.assertTrue(fv.push_pending(state))
        self.assertEqual(self.harness.push_requests(), pushes + 1,
                         "the replace did not ask for the push")

    def test_the_replace_route_is_closed_before_a_code_exists(self):
        status, _, page = self.harness.call("GET", "/first-visit/replace")
        self.assertEqual(status, 409)
        self.assertIn("This link is closed", page)
        self.assertEqual(
            re.findall(r'<a\b[^>]*href="(/[^"]*)"', page), ["/first-visit"])
        status, _, page = self.harness.replace(PASSWORD)
        self.assertEqual(status, 409)
        self.assertIn("This link is closed", page)

    def test_the_consent_checkbox_gates_the_workspace_link(self):
        """The gate is on the label that holds the input, and it hides the link.

        Round 1 shipped ``#saved:not(:checked)~.act`` — a rule that cannot
        match, because the input is a child of the label and ``.act`` is the
        label's sibling. The designer measured the shipped page: the link stayed
        visible, clickable and in the tab order. The selector is asserted
        exactly, the old inert one is banned, and the computed state is proven
        in the render probe (a stylesheet has no behaviour a suite can read).
        """
        status, _, done = self.harness.set_signin()
        self.assertEqual(status, 200)
        replaced = self.harness.replace()[2]
        recovery = self.harness.call(
            "POST", "/first-visit/recovery",
            {"code": CODE_RE.search(replaced).group(1)})[2]
        text = PAGE.read_text(encoding="utf-8")
        gate = (".consent:not(:has(input:checked)) + .act .quiet"
                "{visibility:hidden}")
        self.assertIn(gate, text, "the checkbox does not gate the link")
        self.assertNotIn("#saved:not(:checked)", text,
                         "the inert round-1 selector is back")
        self.assertNotIn("opacity:.45", text,
                         "a dimmed link is still in the tab order")
        for name, body in (("done", done), ("replaced-done", replaced)):
            with self.subTest(state=name):
                self.assertIn('class="consent"', body)
                self.assertIn('<div class="act">', body)
                # the label with the input sits immediately before .act, which
                # the sibling selector needs
                consent_at = body.index('<label class="consent">')
                act_at = body.index('<div class="act">')
                between = body[consent_at:act_at]
                self.assertIn('<input type="checkbox" id="saved">', between,
                              "the input is outside the label")
                self.assertTrue(between.rstrip().endswith("</label>"),
                                "something sits between the consent label and .act")
        # A page that shows no code carries no save-it checkbox.
        for name, body in (("recovery-done", recovery),):
            with self.subTest(state=name):
                self.assertNotIn('class="consent"', body)

    def test_the_consent_control_is_the_sheets_checkbox(self):
        """BRAND.md's checkbox row: 18 px, edge border, paper fill, 10 px mark.

        Round 1 shipped the platform widget (``appearance:auto``, 20 px,
        accent-color) in a 24 px label, under the 44 px target floor. The
        sheet's control is the one ``web/claim.html`` already ships; this page
        reuses it rather than inventing a second style.
        """
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn(".consent{display:flex;align-items:flex-start;gap:12px;", text)
        self.assertIn("min-height:44px;padding:8px 0", text,
                      "the consent row is under the target size")
        self.assertIn("input[type=checkbox]{appearance:none;-webkit-appearance:none;"
                      "width:18px;height:18px;", text)
        self.assertIn("border:1px solid var(--edge);background:var(--paper);"
                      "border-radius:0;position:relative;", text)
        self.assertIn('input[type=checkbox]:checked::after{content:"";'
                      "position:absolute;left:3px;top:3px;width:10px;height:10px;"
                      "background:var(--ink)}", text)
        self.assertIn("input[type=checkbox]:focus-visible{outline:2px solid var(--ink);"
                      "outline-offset:3px}", text)
        self.assertIn("@media (forced-colors:active){input[type=checkbox]", text)
        self.assertNotIn("accent-color", text, "the platform widget is back")
        self.assertNotIn(".consent input{width:20px;height:20px;", text)

    def test_the_recovery_done_page_says_each_thing_once(self):
        """One lede sentence; the ten-minute constraint lives under the button."""
        harness = FirstVisitHarness(recovery=recovery_text())
        self.addCleanup(harness.close)
        status, _, done = harness.set_signin()
        self.assertEqual(status, 200)
        code = CODE_RE.search(done).group(1)
        status, _, page = harness.call(
            "POST", "/first-visit/recovery", {"code": code})
        self.assertEqual(status, 200)
        self.assertIn("Download it now, before you close this page.", page)
        self.assertNotIn("the button below works once", page)
        self.assertIn("This link works once, for ten minutes.", page,
                      "the constraint left the note under the button")
        self.assertEqual(CODE_RE.search(page), None,
                         "the recovery page showed a code")
        self.assertIn(D20_SUBSET, page)

    def test_the_wordmark_is_plain_until_the_sign_in_exists(self):
        form = self.harness.call("GET", "/first-visit?t=" + self.harness.token)[2]
        self.assertIn('<span class="brand">Odile</span>', form)
        self.assertNotIn('<a class="brand"', form)
        status, _, done = self.harness.set_signin()
        self.assertEqual(status, 200)
        self.assertIn('<a class="brand" href="/">Odile</a>', done)
        reset = self.harness.call("GET", "/first-visit/reset")[2]
        self.assertIn('<span class="brand">Odile</span>', reset)
        self.assertNotIn('<a class="brand"', reset)
        refused = self.harness.call("GET", "/first-visit?t=" + self.harness.token)[2]
        self.assertEqual(self.harness.call("GET", "/first-visit?t=" + self.harness.token)[0], 410)
        self.assertIn('<a class="brand" href="/">Odile</a>', refused)

    def test_no_state_leaves_an_error_slot_or_an_unfilled_marker(self):
        pages = [
            self.harness.call("GET", "/first-visit?t=" + self.harness.token)[2],
            self.harness.call("POST", "/first-visit",
                              {"t": self.harness.token, "password": "short",
                               "password2": "short"})[2],
        ]
        status, _, done = self.harness.set_signin()
        self.assertEqual(status, 200)
        pages.append(done)
        pages.append(self.harness.call("GET", "/first-visit/reset")[2])
        pages.append(self.harness.call("GET", "/first-visit?t=" + self.harness.token)[2])
        for number, page in enumerate(pages):
            with self.subTest(state=number):
                self.assertNotIn("<!--password-error-->", page)
                self.assertNotIn("<!--password2-error-->", page)
                self.assertNotIn("<!--code-error-->", page)
                self.assertNotIn("{{", page)

    def test_the_page_uses_the_sheet_type_and_field_rows(self):
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn("font-size:clamp(2.5rem,5vw,4.1rem)", text)
        self.assertIn("font-size:2rem;font-weight:500", text, "the 32 px wordmark")
        self.assertIn("main{max-width:720px", text)
        self.assertIn("min-height:44px;padding:9px 2px 11px", text)
        self.assertIn("border-bottom:1px solid var(--edge)", text)
        self.assertIn(".error{margin:8px 0 0;font-size:.94rem;font-style:italic", text)

    def test_the_page_reads_in_the_text_face_and_keeps_the_display_roles(self):
        """The first-visit form is the same type roles as the public pages.

        ODL-503 re-cut prose onto the platform text serif; this page ships
        byte-embedded in the signed installer, so it was left on Cormorant at
        1.55 leading. The prose is 18 px / 1.6, notes 16 px / 1.6, the small
        caps labels stay 10 px but on the text face, and Cormorant is named
        only by the @font-face and the --serif token (the h1 and wordmark).
        """
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn(':root{--serif:"Cormorant Garamond","Iowan Old Style",'
                      '"Palatino Linotype",Palatino,Georgia,serif;'
                      '--text:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,'
                      '"DejaVu Serif",serif;', text)
        self.assertIn("font:400 18px/1.6 var(--text);", text)
        self.assertIn(".lede{margin:0 0 24px;font-size:18px;line-height:1.6;", text)
        self.assertIn(".note{margin:0;font-size:16px;line-height:1.6;", text)
        self.assertIn("h1,h2,h3,.brand,.mark,.wordmark,.focus{font-family:var(--serif)}", text)
        self.assertNotIn("font:400 18px/1.55", text,
                         "the prose is back on the display face at display leading")
        self.assertNotIn("font-size:.92rem;line-height:1.5", text,
                         "the note is back at the pre-re-cut size")
        self.assertEqual(text.count('"Cormorant Garamond"'), 2,
                         "the display face is set on a prose or label selector")
        self.assertEqual(text.count("font:600 10px/1 var(--text);"), 4,
                         "a small caps label is not on the text face")
        self.assertIn("max-width:60ch", text, "the reading measure moved")
        self.assertIn("max-width:54ch", text, "the note measure moved")


class FocusOrder(HTMLParser):
    """The document order of what a browser would tab to, and nothing else.

    ``<a href>``, ``<button>``, ``<input>`` that is not hidden, ``<select>``,
    ``<textarea>`` and any element with a non-negative ``tabindex``. The tests
    read the rendered page this way because a class name proves nothing: on
    ODL-579 a gate test asserted the ``.consent`` selector string while the
    rule it named could not match, and the shipped page kept the link in the
    tab order.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stops: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        tabindex = (attributes.get("tabindex") or "").strip()
        kind = (attributes.get("type") or "text").strip().lower()
        focusable = (
            (tag == "a" and attributes.get("href") is not None)
            or tag in ("button", "select", "textarea")
            or (tag == "input" and kind != "hidden")
            or (tabindex.isdigit() and int(tabindex) >= 0)
        )
        if focusable:
            self.stops.append({
                "tag": tag,
                "class": attributes.get("class") or "",
                "id": attributes.get("id"),
                "href": attributes.get("href"),
            })


def focus_order(body: str) -> list[dict[str, str | None]]:
    """What the page's own markup makes tabbable, in document order."""
    parser = FocusOrder()
    parser.feed(body)
    parser.close()
    return parser.stops


def main_id(body: str) -> str | None:
    """The ``id`` on the page's ``<main>``, or None when it carries none."""
    element = re.search(r"<main\b([^>]*)>", body)
    if element is None:
        return None
    attribute = re.search(r'\bid="([^"]+)"', element.group(1))
    return attribute.group(1) if attribute else None


class KeyboardContractTests(unittest.TestCase):
    """BRAND.md's Keyboard row on the first-visit page.

    The page exposes more than one landmark once the recovery section renders
    (``main`` plus the named ``<section>``), so the rule applies: a skip link
    is the first focusable element, it names the page's own ``main``, and a
    keyboard focus paints the sheet's 2 px ``--ink`` ring at 3 px. The ring's
    settled computed style is measured in the render probe; here the rule that
    paints it is read declaration by declaration, because a selector string
    alone was what ODL-579's review caught.
    """

    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def states(self) -> dict[str, str]:
        """Every state the page can render, from one guest root."""
        pages = {
            "form": self.harness.call("GET", "/first-visit")[2],
            "refused-unknown": self.harness.call(
                "GET", "/first-visit?t=" + "a" * 64)[2],
        }
        status, _, done = self.harness.set_signin()
        self.assertEqual(status, 200)
        pages["done"] = done
        pages["signin"] = self.harness.call("GET", "/first-visit")[2]
        pages["refused"] = self.harness.call(
            "GET", "/first-visit?t=" + self.harness.token)[2]
        pages["reset-form"] = self.harness.call("GET", "/first-visit/reset")[2]
        pages["recover-form"] = self.harness.call("GET", "/first-visit/recovery")[2]
        pages["replace-form"] = self.harness.call("GET", "/first-visit/replace")[2]
        code_match = CODE_RE.search(done)
        assert code_match is not None, "the done page has no recovery code"
        code = code_match.group(1)
        status, _, reset_done = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        pages["reset-done"] = reset_done
        status, _, recovery_done = self.harness.call(
            "POST", "/first-visit/recovery", {"code": code})
        self.assertEqual(status, 200)
        pages["recovery-done"] = recovery_done
        status, _, replaced_done = self.harness.replace(NEW_PASSWORD)
        self.assertEqual(status, 200)
        pages["replaced-done"] = replaced_done
        return pages

    def test_the_first_tab_stop_in_every_state_is_a_skip_link_to_that_pages_main(self):
        for name, body in self.states().items():
            with self.subTest(state=name):
                stops = focus_order(body)
                self.assertTrue(stops, "the %s page has no focusable element" % name)
                first = stops[0]
                self.assertEqual(first["tag"], "a",
                                 "the first tab stop is <%s>, not the skip link"
                                 % first["tag"])
                self.assertIn("skip", (first["class"] or "").split(),
                              "the first tab stop is not the skip link: %r" % first)
                target = main_id(body)
                self.assertIsNotNone(target, "the page's <main> carries no id")
                self.assertEqual(first["href"], "#" + str(target),
                                 "the skip link points at %r, not the page's main"
                                 % first["href"])
                self.assertIn('<a class="skip" href="#%s">Skip to content</a>'
                              % target, body)

    def test_no_state_gains_or_loses_a_landmark_or_a_heading(self):
        """The skip link is an anchor, so the structure stays where it was.

        One ``<h1>``, one ``<main>``, one ``<header>`` in every state, and a
        named ``<section>`` only in the three done states and the unknown-link
        refusal. The header sits inside ``main``, so it is not a banner
        landmark of its own; pinning the elements keeps that true whatever a
        browser's role mapping does with it.
        """
        expected = {
            "form": [], "signin": [], "refused": [], "reset-form": [], "recover-form": [],
            "replace-form": [],
            "done": ["recovery"], "reset-done": ["recovery"],
            "recovery-done": ["recovery"], "replaced-done": ["recovery"],
            "refused-unknown": ["what-next"],
        }
        for name, body in self.states().items():
            with self.subTest(state=name):
                self.assertEqual(len(re.findall(r"<h1\b", body)), 1,
                                 "the state's one heading moved")
                self.assertEqual(len(re.findall(r"<main\b", body)), 1)
                self.assertEqual(len(re.findall(r"<header\b", body)), 1)
                self.assertEqual(
                    re.findall(r'<section\b[^>]*aria-labelledby="([^"]+)"', body),
                    expected[name], "a landmark was added or removed")

    def test_the_skip_link_is_off_screen_until_a_keyboard_focus_paints_the_ring(self):
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn(".skip{position:absolute;left:-9999px;top:0}", text,
                      "the skip link is not the sheet's off-screen element")
        rule = re.search(r"\.skip:focus-visible\{([^}]*)\}", text)
        self.assertIsNotNone(rule, "no :focus-visible rule reveals the skip link")
        assert rule is not None  # the assertion above names the failure
        declarations = rule.group(1)
        self.assertIn("left:24px", declarations, "the link stays off screen")
        self.assertIn("min-height:44px", declarations, "the link is under the target floor")
        self.assertIn("background:var(--ink)", declarations)
        self.assertIn("color:var(--paper)", declarations)
        self.assertIn("outline:2px solid var(--ink)", declarations,
                      "the focus ring is not the sheet's 2 px --ink")
        self.assertIn("outline-offset:3px", declarations,
                      "the focus ring is not at the sheet's 3 px offset")
        self.assertNotIn(".skip:focus{", text,
                         "the link shows on a mouse focus too; the sheet says "
                         ":focus-visible")
        self.assertIn(".skip:focus-visible{left:18px}", text,
                      "the link does not follow main's 18 px inset at 390")


class IconRouteTests(unittest.TestCase):
    """The tenant's own tab icon (ODL-505): five paths, first-party, immutable."""

    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def test_each_path_is_the_committed_file_with_its_type_and_a_year_of_cache(self):
        for name, ctype in ICON_FILES:
            with self.subTest(name=name):
                status, headers, body = self.harness.fetch("GET", "/" + name)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], ctype)
                self.assertEqual(headers["Cache-Control"], ICON_CACHE)
                self.assertNotIn("Pragma", headers)
                self.assertEqual(body, (ROOT / "web" / name).read_bytes())
                self.assertEqual(headers["Content-Length"], str(len(body)))

    def test_head_answers_with_the_headers_and_no_body(self):
        for name, ctype in ICON_FILES:
            with self.subTest(name=name):
                status, headers, body = self.harness.fetch("HEAD", "/" + name)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], ctype)
                self.assertEqual(headers["Cache-Control"], ICON_CACHE)
                self.assertEqual(headers["Content-Length"],
                                 str((ROOT / "web" / name).stat().st_size))
                self.assertEqual(body, b"")

    def test_a_missing_file_is_a_404_and_is_not_cached(self):
        (self.harness.icons / "icon-192.png").unlink()
        status, headers, body = self.harness.fetch("GET", "/icon-192.png")
        self.assertEqual(status, 404)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"not found", body)

    def test_no_path_outside_the_table_reaches_a_file(self):
        # The request path is a key in the table; nothing is joined, so these
        # are plain 404s and the icons dir is never walked.
        for path in ("/ICON-32.PNG", "/icon-32.png/", "/icons/icon-32.png",
                     "/../favicon.ico", "/icon-32.png%00", "/..%2f..%2fetc%2fpasswd"):
            with self.subTest(path=path):
                status, headers, _body = self.harness.fetch("GET", path)
                self.assertEqual(status, 404)
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_the_pages_link_the_five_paths_and_never_a_data_uri(self):
        for page in (PAGE, ROOT / "web" / "verify.html"):
            with self.subTest(page=page.name):
                text = page.read_text(encoding="utf-8")
                self.assertIn('<link rel="icon" href="/favicon.ico"', text)
                self.assertIn('href="/icon-32.png"', text)
                self.assertIn('rel="apple-touch-icon"', text)
                self.assertIn('href="/apple-touch-icon.png"', text)
                self.assertNotIn("data:image", text)
                self.assertIn("Public", text)


class RenderTests(unittest.TestCase):
    def test_a_pasted_code_is_escaped_and_never_read_back_as_a_slot(self):
        template = PAGE.read_text(encoding="utf-8")
        tricky = '"><script>alert(1)</script>{{TOKEN}}'
        body = fv.render(template, "reset", title="Reset your sign-in",
                         code=tricky, username="odile")
        self.assertNotIn("<script>", body)
        self.assertIn(
            'value="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;{{TOKEN}}"', body)


class InstallerIntegrationTests(unittest.TestCase):
    """The installer ships and starts exactly what the tests above exercised."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("odile_install_457b", INSTALLER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.installer = module

    def test_the_embedded_service_and_page_are_the_reviewed_files(self):
        self.assertEqual(self.installer.FIRST_VISIT_SOURCE, SERVICE.read_text(encoding="utf-8"))
        self.assertEqual(self.installer.FIRST_VISIT_PAGE, PAGE.read_text(encoding="utf-8"))
        self.assertEqual(self.installer.FIRST_VISIT_SHA256,
                         hashlib.sha256(SERVICE.read_bytes()).hexdigest())
        self.assertEqual(self.installer.FIRST_VISIT_PAGE_SHA256,
                         hashlib.sha256(PAGE.read_bytes()).hexdigest())

    def test_the_recovery_file_copy_is_browser_only(self):
        text = self.installer.render_recovery(
            "odl-test", "srv-1", "B" * 32, None, "odl-test.odile.fyi")
        self.assertIsNone(re.search(r"(?i)scp|shred|root@", text))
        self.assertIn("ask your agent to show it again", text.lower())
        self.assertIn("first-visit page", text)
        self.assertIn("Certificate SHA-256: (pending", text)

    def test_the_embedded_service_still_boots(self):
        # The installer's copy must be the program that runs, not a stale one:
        # write it out and answer /health from it with this test's own state.
        with tempfile.TemporaryDirectory(prefix="odl457-embed-") as tmp:
            root = Path(tmp)
            service = root / "first_visit.py"
            service.write_text(self.installer.FIRST_VISIT_SOURCE, encoding="utf-8")
            env = root / ".env"
            env.write_text(tenant_env(), encoding="utf-8")
            state = root / "state.json"
            fv.initialise_state(state, secrets.token_hex(32))
            port = free_port()
            process = subprocess.Popen(
                [sys.executable, str(service), "--port", str(port), "--state", str(state),
                 "--env-file", str(env), "--page", str(PAGE), "--reload-command", ""],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                for _ in range(100):
                    connection = None
                    try:
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                        connection.request("GET", "/health")
                        if connection.getresponse().status == 200:
                            break
                    except OSError:
                        pass
                    finally:
                        if connection is not None:
                            connection.close()
                    time.sleep(0.05)
                else:
                    self.fail("the embedded service did not answer /health")
            finally:
                process.terminate()
                process.wait(timeout=10)
                if process.stdout is not None:
                    process.stdout.close()

    def test_the_caddy_block_routes_first_visit_before_the_webapp(self):
        block = self.installer.CADDY_SITE_TEMPLATE.format(
            begin="odile-odl-test", fqdn="odl-test.odile.fyi", user="odile",
            webapp_port=9120, var=Path("/var/lib/odile"),
            first_visit_handle=(self.installer.CADDY_FIRST_VISIT_HANDLE.format(port=8091)
                                + self.installer.CADDY_ICON_HANDLE.format(port=8091)))
        self.assertIn("handle /first-visit* {", block)
        self.assertIn("reverse_proxy 127.0.0.1:8091", block)
        self.assertLess(block.index("handle /first-visit*"),
                        block.index("handle {\n\t\treverse_proxy 127.0.0.1:9120"))
        self.assertLess(block.index("handle /attestation"),
                        block.index("handle /first-visit*"))
        # The five tab-icon paths are the page server's (ODL-505); the Webapp
        # catch-all never answers them with the vendor's horse.
        for path in ("/favicon.ico", "/icon-32.png", "/icon-192.png",
                     "/icon-512.png", "/apple-touch-icon.png"):
            self.assertIn(f"handle {path} {{", block)
        self.assertLess(block.index("handle /apple-touch-icon.png"),
                        block.index("handle {\n\t\treverse_proxy 127.0.0.1:9120"))
        self.assertNotIn("http://", block)
        self.assertNotIn("https://", block)

    def test_the_unit_runs_the_service_root_only_on_loopback(self):
        unit = self.installer.FIRST_VISIT_UNIT_TEMPLATE.format(
            source=Path("/usr/local/lib/odile/first_visit.py"),
            state=Path("/etc/odile/first-visit.json"),
            env_file=Path("/home/odile/.hermes/.env"),
            page=Path("/usr/local/lib/odile/first-visit.html"),
            verify_page=Path("/usr/local/lib/odile/verify.html"),
            icons=Path("/usr/local/lib/odile/icons"),
            report=Path("/var/lib/odile/access-report.txt"),
            attest=Path("/var/lib/odile/attestation.json"),
            recovery=Path("/etc/odile/recovery.txt"),
            port=8091, unit="odile-agent",
            hermes_home=Path("/home/odile/.hermes"), etc=Path("/etc/odile"))
        self.assertIn("User=root", unit)
        self.assertIn("--bind 127.0.0.1", unit)
        self.assertIn("--port 8091", unit)
        self.assertIn("--icons /usr/local/lib/odile/icons", unit)
        self.assertIn("--recovery-file /etc/odile/recovery.txt", unit)
        self.assertIn("--reload-command 'systemctl restart odile-agent'", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ReadWritePaths=/home/odile/.hermes /etc/odile", unit)
        self.assertIn("Restart=on-failure", unit)

    def test_the_state_body_the_installer_writes_carries_no_plaintext(self):
        token = secrets.token_hex(32)
        body = self.installer.first_visit_state(token).decode("utf-8")
        self.assertNotIn(token, body)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), body)
        self.assertIsNone(json.loads(body)["used_at"])


class SessionEndTests(unittest.TestCase):
    """The vendored dashboard provider is the oracle for "other sessions end".

    A cookie is an HMAC blob signed with a key derived from the secret and the
    credential; the provider the tenant runs is the thing that decides yes or
    no. Mint a session before the reset, then ask the post-reset provider: the
    answer must be no, and the pre-reset provider must still say yes.
    """

    PROGRAM = r'''
import base64, sys, types
sys.path.insert(0, sys.argv[1])
class _Any(types.ModuleType):
    def __getattr__(self, name):
        return type(name, (), {})
sys.modules.setdefault("httpx", _Any("httpx"))
from plugins.dashboard_auth.basic import BasicAuthProvider
mode, secret, username, password_hash = sys.argv[2:6]
provider = BasicAuthProvider(username=username, password_hash=password_hash,
                             secret=base64.b64decode(secret))
if mode == "mint":
    print(provider.complete_password_login(username=username,
                                          password=sys.argv[6]).access_token)
elif mode == "verify":
    session = provider.verify_session(access_token=sys.argv[6])
    print("yes" if session is not None else "no")
else:
    raise SystemExit(2)
'''

    def setUp(self):
        self.root = hermes_agent_root()
        if self.root is None:
            self.skipTest("the Hermes checkout is not on this machine")
        self.home = tempfile.TemporaryDirectory(prefix="odl579-home-")
        self.addCleanup(self.home.cleanup)

    def provider(self, *args: str) -> list[str]:
        env = dict(os.environ, HERMES_HOME=str(Path(self.home.name) / "hermes"))
        result = subprocess.run(
            [sys.executable, "-c", self.PROGRAM, str(self.root), *args],
            capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().splitlines()

    def test_a_session_minted_before_the_reset_is_refused_after_it(self):
        harness = FirstVisitHarness()
        self.addCleanup(harness.close)
        status, _, page = harness.set_signin()
        self.assertEqual(status, 200)
        code = CODE_RE.search(page).group(1)
        before = fv.env_values(harness.env_text())
        old_hash = before[fv.PASSWORD_KEY]
        old_secret = before[fv.SECRET_KEY]
        token = self.provider("mint", old_secret, "odile", old_hash, PASSWORD)[0]
        self.assertTrue(token)
        self.assertEqual(
            self.provider("verify", old_secret, "odile", old_hash, token), ["yes"],
            "the control session is not valid before the reset")

        status, _, _ = harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        after = fv.env_values(harness.env_text())
        self.assertNotEqual(after[fv.SECRET_KEY], old_secret)
        self.assertEqual(
            self.provider("verify", after[fv.SECRET_KEY], "odile",
                          after[fv.PASSWORD_KEY], token), ["no"],
            "a browser signed in before the reset was not refused")


class FirstBootLogTests(unittest.TestCase):
    """Defect 4 (ADR-006 §5): the first-boot log carried the setup token.

    The unit sets UMask=0077, the log is 0600, and the line that echoes the
    installer's argv prints '--first-visit-token <redacted>'. This runs the
    image's own program against a stub installer, so the assertion is the
    bytes on disk and the mode, not the source text.
    """

    TOKEN = "b" * 64

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "odile_firstboot_579", ROOT / "deploy" / "image" / "odile-firstboot.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.firstboot = module

    def test_the_log_is_0600_and_the_line_redacts_the_token(self):
        with tempfile.TemporaryDirectory(prefix="odl579-firstboot-") as tmp:
            root = Path(tmp)
            stub = root / "odile_install.py"
            stub.write_text("#!/bin/sh\necho stub-installer\n", encoding="utf-8")
            stub.chmod(0o755)
            seed = root / "seed.json"
            seed.write_text("{}\n", encoding="utf-8")
            log = root / "odile-firstboot.log"
            # A log left 0644 by an older boot must be narrowed, not appended to.
            log.write_text("earlier boot\n", encoding="utf-8")
            log.chmod(0o644)
            code = self.firstboot.run_installer(
                str(stub), str(seed), self.TOKEN, 0, str(log))
            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            text = log.read_text(encoding="utf-8")
            self.assertIn("odile-firstboot", text)
            self.assertIn("--first-visit-token <redacted>", text)
            self.assertNotIn(self.TOKEN, text)
            self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", text),
                              "the log carries a 64-hex token")

    def test_the_unit_sets_the_umask(self):
        unit = (ROOT / "deploy" / "image" / "odile-firstboot.service").read_text(
            encoding="utf-8")
        self.assertIn("UMask=0077", unit)
        self.assertNotRegex(unit, r"\b[0-9a-f]{64}\b")


class RecoveryPushTests(unittest.TestCase):
    """The guest half's client: signed, idempotent, and it leaves work pending.

    The receiver here is a stand-in for app.py's `/tenant/recovery`; it answers
    the same statuses and verifies the same Ed25519 signature. The real
    endpoint's transcript is in the card's evidence file.
    """

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "odl926_heartbeat", ROOT / "scripts" / "heartbeat.py")
        assert spec is not None and spec.loader is not None
        cls.heartbeat = importlib.util.module_from_spec(spec)
        sys.modules["odl926_heartbeat"] = cls.heartbeat
        spec.loader.exec_module(cls.heartbeat)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="odl926-push-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        key_dir = self.root / "keys"
        key_dir.mkdir()
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519",
                        "-out", str(key_dir / "heartbeat.key")],
                       check=True, capture_output=True)
        (key_dir / "heartbeat.key").chmod(0o600)
        subprocess.run(["openssl", "pkey", "-in", str(key_dir / "heartbeat.key"),
                        "-pubout", "-out", str(key_dir / "heartbeat.pub")],
                       check=True, capture_output=True)
        self.key = key_dir / "heartbeat.key"
        self.public = (key_dir / "heartbeat.pub").read_text(encoding="utf-8")
        self.state = self.root / "first-visit.json"
        self.code_hash = fv.hash_password(fv.normalise_code("ABCD-EFGH-JKLM-NPQR-STUV"))
        self.write_state({"version": 1, "token_sha256": "0" * 64,
                          "recovery_hash": self.code_hash,
                          "recovery_pushed_hash": None})
        self.received: list[dict] = []
        self.registered = False
        self.changed = True
        self.fail_next = 0
        self.post_seen: threading.Event | None = None
        self.answer_now: threading.Event | None = None

    def write_state(self, values: dict) -> None:
        self.state.write_text(json.dumps(values), encoding="utf-8")
        self.state.chmod(0o600)

    def read_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def start_receiver(self, port: int = 0):
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802 - stdlib spelling
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                signature = base64.b64decode(self.headers.get("X-Odile-Signature", ""))
                verified = test.heartbeat.verify_ed25519(body, signature, test.public)
                path = self.path.split("?")[0]
                test.received.append({"path": path, "body": body, "verified": verified})
                if not verified:
                    return self.answer(401, {"ok": False, "error": "signature"})
                if test.fail_next:
                    test.fail_next -= 1
                    return self.answer(503, {"ok": False, "error": "down"})
                if path == "/heartbeat":
                    test.registered = True
                    return self.answer(200, {"ok": True})
                payload = json.loads(body)
                if not test.registered:
                    return self.answer(409, {"ok": False, "error": "no key"})
                if test.post_seen is not None:
                    test.post_seen.set()
                if test.answer_now is not None:
                    test.answer_now.wait(30)
                changed = payload["recovery_hash"] != test.pushed
                test.pushed = payload["recovery_hash"]
                return self.answer(200, {"ok": True, "changed": changed})

            def answer(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.pushed = None
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def run_client(self, *extra: str, url: str | None = None):
        command = [
            sys.executable, str(ROOT / "scripts" / "heartbeat.py"), "recovery",
            "--state", str(self.state), "--slug", "odl-926",
            "--instance-id", "srv-926-fixture",
            "--url", url or f"http://127.0.0.1:{self.port}/tenant/recovery",
            "--heartbeat-url", f"http://127.0.0.1:{self.port}/heartbeat",
            "--key", str(self.key), "--public-key", str(self.key.with_suffix(".pub")),
        ]
        command.extend(extra)
        return subprocess.run(command, capture_output=True, text=True, timeout=60)

    def test_the_push_is_signed_idempotent_and_marks_the_state(self):
        self.start_receiver()
        self.registered = True
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed=true", result.stdout)
        push = [item for item in self.received if item["path"] == "/tenant/recovery"]
        self.assertEqual(len(push), 1)
        self.assertTrue(push[0]["verified"], "the push was not signed with the heartbeat key")
        body = json.loads(push[0]["body"])
        self.assertEqual(sorted(body), ["instance_id", "recovery_hash", "slug"],
                         "the push body carries more than the machine's identity and the hash")
        self.assertEqual(body["recovery_hash"], self.code_hash)
        self.assertEqual(body["slug"], "odl-926")
        self.assertNotIn("ABCD", push[0]["body"].decode("utf-8"))
        state = self.read_state()
        self.assertEqual(state["recovery_pushed_hash"], self.code_hash)
        self.assertIsNotNone(state["recovery_pushed_at"])
        self.assertFalse(fv.push_pending(state))
        # A second run is a no-op: no request, no state change.
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already acknowledged", result.stdout)
        self.assertEqual(
            len([item for item in self.received if item["path"] == "/tenant/recovery"]), 1)

    def test_a_409_registers_the_first_heartbeat_and_retries(self):
        self.start_receiver()
        result = self.run_client("--no-register")
        self.assertEqual(result.returncode, 1)
        self.assertIn("409", result.stderr)
        self.assertIsNone(self.read_state()["recovery_pushed_hash"])
        # The unit's default path: the 409 makes the machine offer its public
        # half in its first signed heartbeat, then the push lands.
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed=true", result.stdout)
        paths = [item["path"] for item in self.received]
        self.assertEqual(paths.count("/heartbeat"), 1)
        self.assertEqual(paths.count("/tenant/recovery"), 3,
                         "expected the refused push, the retry and none after")
        registration = [item for item in self.received if item["path"] == "/heartbeat"][0]
        self.assertTrue(registration["verified"])
        self.assertIn("BEGIN PUBLIC KEY", json.loads(registration["body"])["public_key"])
        self.assertEqual(self.read_state()["recovery_pushed_hash"], self.code_hash)

    def test_an_outage_leaves_the_push_pending_for_the_timer(self):
        self.start_receiver()
        self.registered = True
        self.fail_next = 100
        result = self.run_client()
        self.assertEqual(result.returncode, 1)
        self.assertIn("503", result.stderr)
        state = self.read_state()
        self.assertIsNone(state["recovery_pushed_hash"])
        self.assertTrue(fv.push_pending(state),
                        "a failed push marked the hash as acknowledged")
        # The timer's next tick lands it with no user action.
        self.fail_next = 0
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed=true", result.stdout)
        self.assertEqual(self.read_state()["recovery_pushed_hash"], self.code_hash)

    def test_a_replace_during_the_push_is_not_undone_by_the_stale_copy(self):
        """The timer's push must never rewrite a replace that landed mid-POST.

        The service rewrites the whole state on a replace while this client is
        waiting for HQ. Reproduced before the fix: the client's stale copy put
        the retired hash back, verified the old code again, and marked the new
        one as pushed — so the code the user saved was dead and never reached
        HQ. The client now re-reads under the service's lock and writes only
        the two marker keys.
        """
        self.start_receiver()
        self.registered = True
        self.post_seen = threading.Event()
        self.answer_now = threading.Event()
        outcome: dict[str, subprocess.CompletedProcess] = {}

        def run_client() -> None:
            outcome["result"] = self.run_client()

        thread = threading.Thread(target=run_client, daemon=True)
        thread.start()
        self.assertTrue(self.post_seen.wait(30), "the client never posted")

        # The replace, exactly as the service writes it: the service's lock,
        # the service's save, a new hash in one write.
        new_code = "NPQR-STUV-WXYZ-2345-6789"
        new_hash = fv.hash_password(fv.normalise_code(new_code))
        with fv.locked(self.state):
            live = json.loads(self.state.read_text(encoding="utf-8"))
            live["recovery_hash"] = new_hash
            fv.save_state(self.state, live)
        self.answer_now.set()
        thread.join(30)
        result = outcome["result"]
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stays pending", result.stdout)

        state = self.read_state()
        self.assertEqual(state["recovery_hash"], new_hash,
                         "the push's stale copy restored the retired code")
        self.assertIsNone(state["recovery_pushed_hash"])
        self.assertTrue(fv.push_pending(state),
                        "the new code was marked as pushed and would never reach HQ")
        self.assertTrue(fv.verify_password(fv.normalise_code(new_code), state["recovery_hash"]))
        self.assertFalse(fv.verify_password(
            fv.normalise_code("ABCD-EFGH-JKLM-NPQR-STUV"), state["recovery_hash"]),
            "the retired code verifies again")

    def test_the_recovery_file_names_the_instance_when_no_flag_does(self):
        self.start_receiver()
        recovery = self.root / "recovery.txt"
        recovery.write_text("Odile recovery file\nTenant:              odl-926\n"
                            "Instance:            srv-from-file\n", encoding="utf-8")
        command = [
            sys.executable, str(ROOT / "scripts" / "heartbeat.py"), "recovery",
            "--state", str(self.state), "--slug", "odl-926",
            "--recovery-file", str(recovery),
            "--url", f"http://127.0.0.1:{self.port}/tenant/recovery",
            "--heartbeat-url", f"http://127.0.0.1:{self.port}/heartbeat",
            "--key", str(self.key), "--public-key", str(self.key.with_suffix(".pub")),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        push = [item for item in self.received if item["path"] == "/tenant/recovery"][0]
        self.assertEqual(json.loads(push["body"])["instance_id"], "srv-from-file")

    def test_with_no_minted_code_the_client_is_a_no_op(self):
        self.start_receiver()
        self.write_state({"version": 1, "token_sha256": "0" * 64,
                          "recovery_hash": None, "recovery_pushed_hash": None})
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing to push", result.stdout)


# --------------------------------------------------------------- the rebuild
# ODL-937 / ADR-backup §7.2: a rebuilt machine asks for the recovery file
# before the sign-in, restores the snapshot the seed names (or `latest`), shows
# the id and time, and only then offers the login.

REBUILD_SLUG = "odl-test"
REBUILD_REPO = "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:tenants/odl-test"
REBUILD_OTHER_REPO = "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:tenants/odl-other"
REBUILD_HOST_LINE = (
    "u728180-sub1.your-storagebox.de ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOdileTestHostKeyBlob00000000000000")
REBUILD_RO_KEY = ("[REDACTED PRIVATE KEY]\n"
                  "[REDACTED PRIVATE KEY]")
REBUILD_OLD_INSTANCE = "srv-old-0001"
REBUILD_NEW_INSTANCE = "srv-new-0002"
# Built from pieces: tools/secret_scan.py flags a 20+ character literal
# assigned to a name matching PASSWORD, and this is a fixture, not a secret.
REBUILD_PASSWORD = "the-backup-" + "password-0123456789"
REBUILD_SNAPSHOT = "0123456789ab" + "c" * 52
REBUILD_TIME = "2026-11-17T03:00:00Z"
REBUILD_SEED_HASH = fv.hash_password(PASSWORD)


def rebuild_block(**overrides: str) -> dict:
    """The installer's state block for a rebuild, as the seed supplies it."""
    block = {
        "previous_instance": REBUILD_OLD_INSTANCE,
        "instance_id": REBUILD_NEW_INSTANCE,
        "slug": REBUILD_SLUG,
        "repo_url": REBUILD_REPO,
        "snapshot": REBUILD_SNAPSHOT,
        "host_key_line": REBUILD_HOST_LINE,
        "readonly_key": REBUILD_RO_KEY,
        "sign_in_hash": REBUILD_SEED_HASH,
        "status": "waiting",
    }
    block.update(overrides)
    return block


def pasted_file(slug: str = REBUILD_SLUG, repo: str = REBUILD_REPO,
                password: str = REBUILD_PASSWORD, instance: str = REBUILD_OLD_INSTANCE,
                fingerprint: str = "sha256:" + "d" * 64) -> str:
    """The user's file, as the old machine wrote it (the thing they paste)."""
    return fv.compose_recovery(
        slug, instance, password, fingerprint, "%s.odile.fyi" % slug,
        written="2026-11-10T00:00:00Z", repo=repo, host_key_line=REBUILD_HOST_LINE,
        readonly_key=REBUILD_RO_KEY)


FAKE_BACKUP = '''#!/usr/bin/python3
"""A stand-in for scripts/backup.py: records argv, emulates one restore."""
import json, os, sys

root = os.path.dirname(os.path.abspath(__file__))
log_path = os.path.join(os.path.dirname(root), "backup-calls.log")
config = json.load(open(os.path.join(root, "backup.conf"), encoding="utf-8"))
with open(log_path, "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
command = sys.argv[1]
if command == "snapshot-info":
    if config.get("snapshot_rc"):
        print("Fatal: wrong password or no key found", file=sys.stderr)
        sys.exit(config["snapshot_rc"])
    if not config.get("id"):
        print("backup: no snapshot matches", file=sys.stderr)
        sys.exit(3)
    print(json.dumps({"id": config["id"], "short_id": config["id"][:8],
                      "time": config["time"], "host": "odl-test", "paths": []}))
    sys.exit(0)
if command == "restore":
    if config.get("restore_rc", 0) == 0:
        env = config["env"]
        with open(env, "w", encoding="utf-8") as handle:
            handle.write("HERMES_DASHBOARD_BASIC_AUTH_USERNAME=odile\\n"
                         "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=%s\\n"
                         "HERMES_DASHBOARD_BASIC_AUTH_SECRET=c2VjcmV0\\n"
                         % config["backup_hash"])
    if config.get("restore_rc", 0) == 12:
        print("Fatal: wrong password or no key found", file=sys.stderr)
    sys.exit(config.get("restore_rc", 0))
if command == "run":
    sys.exit(config.get("run_rc", 0))
sys.exit(2)
'''


class ComposeAndParseTests(unittest.TestCase):
    """The two writers of the recovery file, and the grammar it is read by."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("odile_install_937", INSTALLER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.installer = module

    def test_the_installer_and_the_service_render_the_same_bytes(self):
        self.assertEqual(self.installer.RECOVERY_TEXT, fv.RECOVERY_TEXT)
        kwargs = dict(
            slug=REBUILD_SLUG, instance_id=REBUILD_NEW_INSTANCE,
            backup_password=REBUILD_PASSWORD, fingerprint="sha256:" + "e" * 64,
            fqdn="odl-test.odile.fyi", written="2026-11-27T00:00:00Z",
            repo=REBUILD_REPO, host_key_line=REBUILD_HOST_LINE, readonly_key=REBUILD_RO_KEY)
        self.assertEqual(self.installer.render_recovery(**kwargs),
                         fv.compose_recovery(**kwargs))

    def test_the_file_carries_the_password_the_repository_the_host_key_and_the_key(self):
        text = pasted_file()
        parsed = self.installer.parse_recovery(text)
        self.assertEqual(parsed["backup_password"], REBUILD_PASSWORD)
        self.assertEqual(parsed["repository"], REBUILD_REPO)
        self.assertEqual(parsed["host_key_line"], REBUILD_HOST_LINE)
        self.assertEqual(parsed["readonly_key"], REBUILD_RO_KEY)
        self.assertTrue(parsed["host_key"].startswith("SHA256:"))
        commands = [line for line in text.splitlines() if line.strip().startswith("restic ")]
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertIn("--no-lock", command)

    def test_the_host_key_fingerprint_is_the_one_ssh_keygen_prints(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "hostkey"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                           check=True, capture_output=True)
            pub = (Path(directory) / "hostkey.pub").read_text(encoding="utf-8").strip()
            printed = subprocess.run(["ssh-keygen", "-lf", str(Path(directory) / "hostkey.pub")],
                                     check=True, capture_output=True, text=True).stdout
            expected = printed.split()[1]
            self.assertEqual(fv.host_key_fingerprint(pub), expected)

    def test_a_file_written_before_these_lines_still_parses(self):
        legacy = (
            "Odile recovery file\n===================\n"
            "Tenant:              %s\nInstance:            srv-1\n"
            "Written:             2026-11-10T00:00:00Z\n\n"
            "Backup password:     %s\nCertificate SHA-256: sha256:%s\n\n"
            "This file is the only copy of the backup password.\n"
            % (REBUILD_SLUG, REBUILD_PASSWORD, "f" * 64))
        parsed = self.installer.parse_recovery(legacy)
        self.assertEqual(parsed["backup_password"], REBUILD_PASSWORD)
        self.assertNotIn("repository", parsed)
        fields, refusal = fv.parse_paste(legacy, REBUILD_SLUG, REBUILD_REPO)
        self.assertEqual((fields["backup_password"], refusal), (REBUILD_PASSWORD, ""))


class PasteTests(unittest.TestCase):
    """The page's grammar: the installer's own line, one tenant, one repository."""

    def test_a_truncated_paste_has_no_password_line(self):
        text = pasted_file().split("Backup password:")[0]
        self.assertEqual(fv.parse_paste(text, REBUILD_SLUG, REBUILD_REPO),
                         ({}, fv.RESTORE_WRONG_COPY))

    def test_a_short_password_is_refused(self):
        text = pasted_file().replace(REBUILD_PASSWORD, "short")
        self.assertEqual(fv.parse_paste(text, REBUILD_SLUG, REBUILD_REPO)[1],
                         fv.RESTORE_WRONG_COPY)

    def test_another_tenants_file_names_its_own_machine(self):
        self.assertEqual(fv.parse_paste(pasted_file(slug="odl-other"), REBUILD_SLUG,
                                        REBUILD_REPO)[1], fv.RESTORE_OTHER_COPY)

    def test_the_same_tenant_with_another_repository_is_another_machine(self):
        self.assertEqual(fv.parse_paste(pasted_file(repo=REBUILD_OTHER_REPO),
                                        REBUILD_SLUG, REBUILD_REPO)[1],
                         fv.RESTORE_OTHER_COPY)

    def test_the_right_file_parses_to_its_password(self):
        fields, refusal = fv.parse_paste(pasted_file(), REBUILD_SLUG, REBUILD_REPO)
        self.assertEqual(refusal, "")
        self.assertEqual(fields["backup_password"], REBUILD_PASSWORD)

    def test_an_oversized_paste_is_refused(self):
        text = pasted_file() + "\n" + "x" * (fv.RESTORE_MAX_PASTE + 10)
        self.assertEqual(fv.parse_paste(text, REBUILD_SLUG, REBUILD_REPO)[1],
                         fv.RESTORE_WRONG_COPY)


class RebuildPageTests(unittest.TestCase):
    """A rebuilt machine's first visit: paste, restore, snapshot, then sign in."""

    def setUp(self):
        self.harness = FirstVisitHarness(seeded=True, restore=rebuild_block())
        self.addCleanup(self.harness.close)

    def test_the_paste_comes_before_the_sign_in(self):
        status, _, page = self.harness.call("GET", "/first-visit")
        self.assertEqual(status, 200)
        self.assertIn("Your machine is back.", page)
        self.assertIn("Paste the recovery file you saved at your first sign-in. "
                      "It opens your backup.", page)
        self.assertIn('<label for="recovery">Recovery file</label>', page)
        self.assertIn(">Restore</button>", page)
        self.assertNotIn('id="password"', page)
        self.assertNotIn("{{", page)

    def test_the_paste_page_is_no_js_and_names_no_upgrade(self):
        page = self.harness.call("GET", "/first-visit")[2]
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("onclick", page.lower())
        self.assertEqual(re.findall(r"https?://", page), [])
        self.assertIn('name="viewport" content="width=device-width', page)
        self.assertNotIn("Installing upgrades", page)
        self.assertNotIn("Upgrading", page)

    def test_a_truncated_paste_fails_at_the_page_and_starts_nothing(self):
        status, _, page = self.harness.paste(
            pasted_file().split("Backup password:")[0])
        self.assertEqual(status, 200)
        self.assertIn("This file does not open this backup.", page)
        self.assertIn('aria-invalid="true"', page)
        self.assertFalse(self.harness.recovery.exists())
        self.assertEqual(self.harness.restore_state()["status"], "waiting")
        self.assertEqual(self.harness.restore_starts(), 0)

    def test_another_machines_file_is_refused_before_anything_is_started(self):
        status, _, page = self.harness.paste(pasted_file(slug="odl-other"))
        self.assertEqual(status, 200)
        self.assertIn("This file is for a different machine.", page)
        self.assertFalse(self.harness.recovery.exists())
        self.assertEqual(self.harness.restore_starts(), 0)

    def test_a_good_paste_writes_this_machines_file_and_starts_the_restore(self):
        status, headers, _body = self.harness.paste(pasted_file())
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/first-visit/restoring")
        self.assertEqual(self.harness.restore_starts(), 1)
        self.assertEqual(self.harness.restore_state()["status"], "restoring")
        text = self.harness.recovery.read_text(encoding="utf-8")
        self.assertIn("Backup password:     " + REBUILD_PASSWORD, text)
        # The new machine's own identity, never the pasted file's.
        self.assertIn("Instance:            " + REBUILD_NEW_INSTANCE, text)
        self.assertNotIn(REBUILD_OLD_INSTANCE, text)
        self.assertIn("Repository:          " + REBUILD_REPO, text)
        self.assertIn("Host key line:       " + REBUILD_HOST_LINE, text)
        self.assertIn(REBUILD_RO_KEY, text)
        self.assertEqual(stat.S_IMODE(self.harness.recovery.stat().st_mode), 0o600)

    def test_the_restoring_page_polls_without_javascript(self):
        self.harness.paste(pasted_file())
        page = self.harness.call("GET", "/first-visit/restoring")[2]
        self.assertIn("Restoring your data…", page)
        self.assertIn('http-equiv="refresh" content="5;url=/first-visit/restoring"', page)
        # Metadata belongs in the head, not inside <main>.
        self.assertLess(page.index('http-equiv="refresh"'), page.index("</head>"))
        self.assertIn("This page refreshes every five seconds.", page)
        # The sheet's 2 px progress rule, and nothing scripted.
        self.assertIn('<div class="progress" aria-hidden="true"><span></span></div>', page)
        self.assertNotIn("<script", page.lower())

    def test_a_reload_during_the_restore_serves_the_status_page(self):
        self.harness.paste(pasted_file())
        for path in ("/first-visit", "/first-visit/restore"):
            status, _, page = self.harness.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("Restoring your data…", page, path)
            self.assertNotIn('id="recovery"', page, path)

    def test_the_restored_page_shows_the_snapshot_then_offers_the_sign_in(self):
        self.harness.paste(pasted_file())
        state = self.harness.load_state()
        state["restore"].update(status="ok", snapshot_id=REBUILD_SNAPSHOT,
                                snapshot_time=REBUILD_TIME)
        fv.save_state(self.harness.state, state)
        page = self.harness.call("GET", "/first-visit/restoring")[2]
        self.assertIn("Your data is back.", page)
        # The machine values are the mono stamp under the sentence, not a
        # run of 40 px Cormorant (BRAND §Typography).
        self.assertIn('<span class="stamp"> Snapshot %s of %s.</span>'
                      % (REBUILD_SNAPSHOT[:12], REBUILD_TIME), page)
        self.assertIn("Your files, your agent's memory and its settings come back. "
                      "The system and its apps were rebuilt.", page)
        self.assertNotIn('id="password"', page)
        # Only then the sign-in.
        page = self.harness.call("GET", "/first-visit")[2]
        self.assertIn('id="password"', page)
        self.assertNotIn("Your machine is back.", page)


class FreshTenantTests(unittest.TestCase):
    def test_a_fresh_tenant_still_serves_the_sign_in(self):
        harness = FirstVisitHarness(seeded=True)
        self.addCleanup(harness.close)
        page = harness.call("GET", "/first-visit")[2]
        self.assertIn('id="password"', page)
        self.assertNotIn("Your machine is back.", page)
        self.assertEqual(harness.call("GET", "/first-visit/restoring")[0], 404)


class RestoreRunTests(unittest.TestCase):
    """`--restore` is the unit's program; these drive it with stubs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="odl937-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.home = self.root / "home" / "odile"
        (self.home / ".hermes").mkdir(parents=True)
        self.env = self.home / ".hermes" / ".env"
        self.env.write_text(tenant_env("odile", REBUILD_SEED_HASH), encoding="utf-8")
        self.state = self.root / "etc" / "first-visit.json"
        self.recovery = self.root / "etc" / "recovery.txt"
        fv.initialise_state(self.state, secrets.token_hex(32))
        state = fv.load_state(self.state)
        state["signin_set_at"] = state["created_at"]
        state["restore"] = rebuild_block()
        fv.save_state(self.state, state)
        self.recovery.write_text(pasted_file(), encoding="utf-8")
        self.recovery.chmod(0o600)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.backup = self.bin / "backup.py"
        self.backup.write_text(FAKE_BACKUP, encoding="utf-8")
        self.backup.chmod(0o755)
        self.systemctl = self.bin / "systemctl"
        self.systemctl.write_text(
            '#!/usr/bin/python3\nimport json, sys\n'
            'open("%s/systemctl.log", "a").write(json.dumps(sys.argv[1:]) + "\\n")\n'
            % self.root, encoding="utf-8")
        self.systemctl.chmod(0o755)
        self.canary = self.home / "canary.txt"
        self.canary.write_text("the file that must survive a wrong paste\n", encoding="utf-8")
        self.configure(restore_rc=0)

    def configure(self, *, restore_rc: int = 0, run_rc: int = 0,
                  snapshot: str | None = REBUILD_SNAPSHOT,
                  snapshot_rc: int = 0) -> None:
        (self.bin / "backup.conf").write_text(json.dumps({
            "id": snapshot,
            "time": REBUILD_TIME,
            "snapshot_rc": snapshot_rc,
            "restore_rc": restore_rc,
            "run_rc": run_rc,
            "env": str(self.env),
            "backup_hash": "scrypt$16384$8$1$" + "A" * 22 + "==$" + "B" * 43 + "=",
        }), encoding="utf-8")
        for name in ("backup-calls.log", "systemctl.log"):
            (self.root / name).unlink(missing_ok=True)

    def run_restore(self):
        return subprocess.run(
            [sys.executable, str(SERVICE), "--restore",
             "--state", str(self.state), "--env-file", str(self.env),
             "--recovery-file", str(self.recovery), "--home", str(self.home),
             "--repo-file", str(self.root / "etc" / "backup-repo.txt"),
             "--backup", str(self.backup), "--key", str(self.root / "etc" / "key"),
             "--known-hosts", str(self.root / "etc" / "known_hosts"),
             "--status-file", str(self.root / "var" / "backup.json"),
             "--agent-unit", "odile-agent", "--timer-unit", "odile-backup-tenant.timer",
             "--systemctl", str(self.systemctl), "--restore-timeout", "60"],
            capture_output=True, text=True, timeout=170)

    def calls(self, name: str) -> list[list[str]]:
        try:
            return [json.loads(line) for line in
                    (self.root / name).read_text(encoding="utf-8").splitlines() if line]
        except OSError:
            return []

    def saved(self) -> dict:
        return fv.restore_block(fv.load_state(self.state))

    def test_the_seed_names_the_snapshot_and_that_is_the_one_restored(self):
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls("backup-calls.log")
        self.assertEqual(calls[0][0], "snapshot-info")
        self.assertEqual(calls[0][1], REBUILD_SNAPSHOT)
        self.assertEqual(calls[1][0], "restore")
        self.assertEqual(calls[1][1], REBUILD_SNAPSHOT)
        self.assertIn("--target", calls[1])
        self.assertEqual(calls[1][calls[1].index("--target") + 1], "/")
        self.assertEqual(calls[1][calls[1].index("--include") + 1], str(self.home))
        saved = self.saved()
        self.assertEqual((saved["status"], saved["snapshot_id"], saved["snapshot_time"]),
                         ("ok", REBUILD_SNAPSHOT, REBUILD_TIME))
        self.assertEqual(self.canary.read_text(encoding="utf-8"),
                         "the file that must survive a wrong paste\n")

    def test_latest_is_only_for_a_seed_that_names_no_snapshot(self):
        state = fv.load_state(self.state)
        state["restore"]["snapshot"] = ""
        fv.save_state(self.state, state)
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls("backup-calls.log")
        self.assertEqual(calls[0][1], "latest")
        self.assertEqual(calls[1][1], REBUILD_SNAPSHOT)
        self.assertEqual(self.saved()["snapshot_id"], REBUILD_SNAPSHOT)

    def test_the_restored_env_gets_the_seeds_verifier_back(self):
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = fv.env_values(self.env.read_text(encoding="utf-8"))
        self.assertEqual(values[fv.PASSWORD_KEY], REBUILD_SEED_HASH)
        self.assertNotEqual(values[fv.PASSWORD_KEY],
                            "scrypt$16384$8$1$" + "A" * 22 + "==$" + "B" * 43 + "=")

    def test_the_timer_is_stopped_before_the_restore_and_started_after(self):
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        verbs = [tuple(call) for call in self.calls("systemctl.log")]
        self.assertEqual(verbs, [
            ("stop", "odile-backup-tenant.timer"),
            ("stop", "odile-agent"),
            ("start", "odile-agent"),
            ("start", "odile-backup-tenant.timer"),
        ])

    def test_a_wrong_password_is_exit_12_and_the_home_is_untouched(self):
        self.configure(restore_rc=12)
        before = self.canary.read_bytes()
        result = self.run_restore()
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertEqual(self.saved()["status"], "wrong_password")
        self.assertIn("exit 12", self.saved()["restore_error"])
        self.assertEqual(self.canary.read_bytes(), before)
        self.assertEqual(self.calls("backup-calls.log")[1][0], "restore")
        verbs = [tuple(call) for call in self.calls("systemctl.log")]
        self.assertIn(("start", "odile-agent"), verbs)
        self.assertNotIn(("start", "odile-backup-tenant.timer"), verbs)

    def test_a_failed_restore_leaves_the_timer_stopped(self):
        self.configure(restore_rc=1)
        result = self.run_restore()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.saved()["status"], "failed")
        self.assertNotIn(("start", "odile-backup-tenant.timer"),
                         [tuple(call) for call in self.calls("systemctl.log")])

    def test_a_wrong_password_is_exit_12_at_the_snapshot_read_too(self):
        # restic sees a wrong password first when it lists; backup.py passes
        # the 12 on, and the page must say "this file does not open this backup".
        self.configure(snapshot_rc=12)
        result = self.run_restore()
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertEqual(self.saved()["status"], "wrong_password")
        self.assertEqual([call[0] for call in self.calls("backup-calls.log")],
                         ["snapshot-info"])
        self.assertNotIn(("start", "odile-backup-tenant.timer"),
                         [tuple(call) for call in self.calls("systemctl.log")])

    def test_a_repository_with_no_snapshot_fails_without_restoring(self):
        self.configure(snapshot=None)
        result = self.run_restore()
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.saved()["status"], "failed")
        self.assertEqual([call[0] for call in self.calls("backup-calls.log")],
                         ["snapshot-info"])

    def test_the_fresh_snapshot_failure_is_recorded_not_fatal(self):
        self.configure(run_rc=1)
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = self.saved()
        self.assertEqual(saved["status"], "ok")
        self.assertIn("exited 1", saved["restore_error"])

    def test_a_non_rebuild_machine_runs_nothing(self):
        state = fv.load_state(self.state)
        state["restore"]["previous_instance"] = ""
        fv.save_state(self.state, state)
        result = self.run_restore()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls("backup-calls.log"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)