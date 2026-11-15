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
from pathlib import Path

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


def tenant_env(username: str = "odile") -> str:
    """The installer's three-key allowlist, with a placeholder hash."""
    return (
        "# Odile tenant .env — an allowlist, not a copy of a seat .env.\n"
        "# Exactly these three keys.\n"
        f"HERMES_DASHBOARD_BASIC_AUTH_USERNAME={username}\n"
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt$16384$8$1$AAAA$BBBB\n"
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET=c2VjcmV0LXBsYWNlaG9sZGVy\n"
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
    """One service process, one temp guest root, one token."""

    def __init__(self, reload_command: str = "", username: str = "odile",
                 recovery: str | None = None, caddy_stores: list[Path] | None = None):
        self.tmp = tempfile.TemporaryDirectory(prefix="odl457-")
        self.root = Path(self.tmp.name)
        self.hermes = self.root / "home" / "odile" / ".hermes"
        self.hermes.mkdir(parents=True)
        self.env = self.hermes / ".env"
        self.env.write_text(tenant_env(username), encoding="utf-8")
        self.env.chmod(0o600)
        self.state = self.root / "etc" / "first-visit.json"
        self.recovery = self.root / "etc" / "recovery.txt"
        self.token = secrets.token_hex(32)
        fv.initialise_state(self.state, self.token)
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
        return self.call("POST", "/first-visit",
                         {"t": self.token, "password": password, "password2": password},
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

    def download(self, code: str, headers: dict[str, str] | None = None):
        return self.call("GET", "/first-visit/recovery?c=" + code, headers=headers)

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
        """ODL-521's boundary: the session secret is ODL-522's, not this card's."""
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
    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def test_a_valid_token_renders_the_form(self):
        status, headers, page = self.harness.call("GET", "/first-visit?t=" + self.harness.token)
        self.assertEqual(status, 200)
        self.assertIn("Set your sign-in", page)
        self.assertIn('name="t" value="%s"' % self.harness.token, page)
        self.assertNotIn("{{", page)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("form-action 'self'", headers.get("Content-Security-Policy", ""))

    def test_a_reload_does_not_spend_the_token(self):
        # A mail scanner prefetches the link and a phone reloads the tab; both
        # are GETs. The token is spent by the POST, not the render.
        for _ in range(3):
            status, _, _ = self.harness.call("GET", "/first-visit?t=" + self.harness.token)
            self.assertEqual(status, 200)
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)

    def test_a_missing_or_unknown_token_is_refused(self):
        for path in ("/first-visit", "/first-visit?t=", "/first-visit?t=" + "a" * 64,
                     "/first-visit?t=short"):
            with self.subTest(path=path):
                status, _, page = self.harness.call("GET", path)
                self.assertEqual(status, 404)
                self.assertIn("This link is closed", page)
                self.assertNotIn("Set your sign-in", page)

    def test_a_reused_token_is_refused(self):
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)
        first_hash = self.harness.stored_hash()
        self.assertEqual(self.harness.call("GET", "/first-visit?t=" + self.harness.token)[0], 410)
        status, _, page = self.harness.set_signin("another-long-password-0001")
        self.assertEqual(status, 410)
        self.assertIn("This link is closed", page)
        self.assertEqual(self.harness.stored_hash(), first_hash,
                         "a refused reuse must not rewrite the hash")
        self.assertEqual(self.harness.load_state()["used_at"],
                         self.harness.load_state()["signin_set_at"])

    def test_two_posts_at_once_spend_the_token_once(self):
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
        self.assertEqual(sorted(results), [200, 410], results)


class RefusedPageTests(unittest.TestCase):
    """A refused link must not offer an action that refuses (ODL-471 follow-up).

    An unknown token on a machine whose sign-in was never set means no recovery
    code has been issued, so ``/first-visit/reset`` answers 409. A spent token
    means the code exists, and the reset form is the way back.
    """

    def setUp(self):
        self.harness = FirstVisitHarness()
        self.addCleanup(self.harness.close)

    def test_an_unknown_token_offers_no_action_that_refuses(self):
        for path in ("/first-visit", "/first-visit?t=short",
                     "/first-visit?t=" + "a" * 64):
            with self.subTest(path=path):
                status, _, page = self.harness.call("GET", path)
                self.assertEqual(status, 404)
                self.assertIn("This link is closed", page)
                self.assertNotIn("/first-visit/reset", page)
                self.assertNotIn('href="/first-visit/reset"', page)
                self.assertNotIn("Reset with a recovery code", page)
                # Nothing left to click can refuse: the wordmark is plain
                # before the sign-in exists and no other first-visit route is
                # offered. `<link rel=icon>` is not a click, so the scan is
                # scoped to anchors (ODL-504).
                self.assertEqual(re.findall(r'<a\b[^>]*href="(/[^"]*)"', page), [])
                self.assertIn("ready email", page)
                self.assertIn("help@odile.fyi", page)
        # The route the page used to offer still refuses; that is why it is gone.
        self.assertEqual(self.harness.call("GET", "/first-visit/reset")[0], 409)

    def test_a_spent_token_still_offers_the_reset_form(self):
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

    def test_an_unknown_token_keeps_the_reset_link_once_a_code_exists(self):
        # The sign-in is set, so a recovery code exists and the reset route
        # answers 200 even though this token does not match.
        status, _, _ = self.harness.set_signin()
        self.assertEqual(status, 200)
        status, _, page = self.harness.call("GET", "/first-visit?t=" + "b" * 64)
        self.assertEqual(status, 404)
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
        self.assertIn("hashed there. We never see it.", PAGE.read_text(encoding="utf-8"))
        self.assertTrue(fv.verify_password(PASSWORD, self.harness.stored_hash()))
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(PASSWORD, self.harness.state.read_text(encoding="utf-8"))
        after = self.harness.env.stat()
        self.assertEqual(after.st_mode & 0o777, 0o600, "the .env must stay 0600")
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        self.assertNotIn(PASSWORD, page)
        self.assertEqual(headers.get("Cache-Control"), "no-store")

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
        # Re-fetching the page cannot show it again: the token is spent.
        status, _, page = self.harness.call("GET", "/first-visit?t=" + self.harness.token)
        self.assertEqual(status, 410)
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

    def test_the_reset_works_and_rotates_the_code(self):
        old_code = self.sign_in_and_read_code()
        status, headers, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": old_code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        new_match = CODE_RE.search(page)
        self.assertIsNotNone(new_match, "a reset must show the new recovery code once")
        new_code = new_match.group(1)
        self.assertNotEqual(new_code, old_code)
        self.assertTrue(fv.verify_password(NEW_PASSWORD, self.harness.stored_hash()))
        self.assertFalse(fv.verify_password(PASSWORD, self.harness.stored_hash()))
        state = self.harness.load_state()
        self.assertFalse(fv.verify_password(fv.normalise_code(old_code), state["recovery_hash"]))
        self.assertTrue(fv.verify_password(fv.normalise_code(new_code), state["recovery_hash"]))
        self.assertIsNotNone(state["recovery_used_at"])
        self.assertNotIn(NEW_PASSWORD, self.harness.env_text())
        # The rotated-out code is dead; the new one still opens the reset form.
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": old_code, "password": "third-long-password-0003",
             "password2": "third-long-password-0003"})
        self.assertEqual(status, 200)
        self.assertIn("That recovery code does not match.", page)
        status, _, _ = self.harness.call("GET", "/first-visit/reset")
        self.assertEqual(status, 200)

    def test_a_pasted_code_with_spaces_and_lowercase_works(self):
        code = self.sign_in_and_read_code()
        pasted = " " + code.lower().replace("-", " ") + " "
        status, _, page = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": pasted, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(fv.verify_password(NEW_PASSWORD, self.harness.stored_hash()))


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
        status, headers, body = harness.download(code)
        self.assertEqual(status, 404)
        self.assertIn("text/plain", headers.get("Content-Type", ""))
        self.assertTrue(body.strip(), "a 404 must still carry a body")

    def test_an_empty_recovery_file_is_unavailable_never_an_empty_download(self):
        harness = self.harness(recovery="")
        code, page = self.signed_in(harness)
        self.assertNotIn('class="download"', page)
        self.assertIn("The recovery file is not available on this machine.", page)
        status, _, body = harness.download(code)
        self.assertEqual(status, 404)
        self.assertTrue(body.strip())

    def test_the_done_page_offers_the_file_beside_the_recovery_code(self):
        harness = self.harness(recovery=recovery_text())
        code, page = self.signed_in(harness)
        self.assertIn('class="download"', page)
        self.assertIn('href="/first-visit/recovery?c=%s"' % code, page)
        self.assertIn('download="odile-recovery-odl-test.txt"', page)
        self.assertLess(page.index('id="code"'), page.index('class="download"'))
        self.assertIn(A9_DECIDED, page)

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
        for number, (page, warning) in enumerate((
                (done, "This is the only time the code is shown."),
                (reset_done, "This is the only time the new code is shown."))):
            with self.subTest(state=number):
                self.assertIn('class="download"', page)
                self.assertNotIn('class="primary"', page,
                                 "the download is the done page's one filled control")
                self.assertLess(page.index(warning), page.index('class="download"'),
                                "the warning sits above the download")
        refused = harness.call("GET", "/first-visit?t=" + "a" * 64)[2]
        self.assertEqual(refused.count('class="primary"'), 1,
                         "the refused state keeps its one filled link")

    def test_the_download_is_the_recovery_file_text_plain_with_a_filename(self):
        harness = self.harness(recovery=recovery_text())
        code, _ = self.signed_in(harness)
        before = harness.recovery.stat()
        status, headers, body = harness.download(code)
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
            self.assertNotIn("pending on this machine", page)
            on_disk = harness.recovery.read_text(encoding="utf-8")
            self.assertIn("Certificate SHA-256: " + expected, on_disk)
            self.assertNotIn(PLACEHOLDER, on_disk)
            written = harness.recovery.stat().st_mtime_ns

            status, _, body = harness.download(code, headers=headers)
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
            self.assertIn("certificate fingerprint is pending on this machine", page)
            self.assertIn(PLACEHOLDER, harness.recovery.read_text(encoding="utf-8"))
            status, _, body = harness.download(code, headers=headers)
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
            self.assertNotIn("pending on this machine", page,
                             "a chain file is a certificate; the page says so")
            on_disk = harness.recovery.read_text(encoding="utf-8")
            self.assertIn("Certificate SHA-256: " + leaf, on_disk)
            self.assertNotIn(chained, on_disk)
            status, _, body = harness.download(code, headers=headers)
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

    def test_the_download_requires_the_recovery_code(self):
        harness = self.harness(recovery=recovery_text())
        code, _ = self.signed_in(harness)
        for wrong in ("AAAA-BBBB-CCCC-DDDD-EEEE", ""):
            status, _, body = harness.download(wrong)
            self.assertEqual(status, 404)
            self.assertNotIn(BACKUP_PASSWORD, body)
        status, _, body = harness.download(code)
        self.assertEqual(status, 200)
        self.assertIn(BACKUP_PASSWORD, body)

    def test_the_reset_done_page_offers_the_new_code(self):
        harness = self.harness(recovery=recovery_text())
        old_code, _ = self.signed_in(harness)
        status, _, page = harness.call(
            "POST", "/first-visit/reset",
            {"code": old_code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        match = CODE_RE.search(page)
        assert match is not None
        new_code = match.group(1)
        self.assertIn('href="/first-visit/recovery?c=%s"' % new_code, page)
        self.assertEqual(harness.download(old_code)[0], 404)
        status, _, body = harness.download(new_code)
        self.assertEqual(status, 200)
        self.assertIn(BACKUP_PASSWORD, body)


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
            connection.request("HEAD", "/first-visit?t=" + self.harness.token)
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
        ]
        status, _, done = self.harness.set_signin()
        self.assertEqual(status, 200)
        pages.append(done)
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

    def test_the_form_asks_for_the_password_twice_and_hints_the_length(self):
        page = self.harness.call("GET", "/first-visit?t=" + self.harness.token)[2]
        self.assertIn('id="password" name="password"', page)
        self.assertIn('id="password2" name="password2"', page)
        self.assertIn("At least 16 characters.", page)
        self.assertIn('aria-describedby="password-hint"', page)

    def test_the_recovery_screens_carry_a9_and_no_coined_custody_sentence(self):
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn(A9_DECIDED, text)
        self.assertNotIn("We hold neither", text)
        self.assertNotIn("Only its hash stays on this machine.", text)
        self.assertIn("A7 and A9", text)
        status, _, done = self.harness.set_signin()
        self.assertEqual(status, 200)
        self.assertIn(A9_DECIDED, done)
        match = CODE_RE.search(done)
        assert match is not None, "the done page has no recovery code"
        code = match.group(1)
        status, _, reset_done = self.harness.call(
            "POST", "/first-visit/reset",
            {"code": code, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn(A9_DECIDED, reset_done)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
