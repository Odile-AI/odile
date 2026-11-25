#!/usr/bin/python3
"""Tests for the recovery kit generator, re-cut for the strict bar.

The kit attached to the ready email is the machine's own document: its facts,
the tokenless first-sign-in page, and the two ways back (the recovery code and
the recovery file). The code is stable — it does not rotate on use and does not
expire; only a replace from the machine mints a new one (D-20, ODL-926) — and
the kit prints that rule, not the rotation line it retired. It carries no
terminal, no key the user handles and no passphrase — A9, A7 and A5 of
`docs/product/CLAIMS-RECUT-STRICT.md`.

Hermetic. The render tests call the real CLI; the seed test starts the real
seed service against a throwaway database and key, issues a token, renders the
kit, and proves the token still works once at the seed exchange — the kit no
longer prints it (ODL-925). No machine, no network beyond loopback, no live
database.

    /usr/bin/python3 -m unittest tests.test_recovery_kit -v
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import importlib.util
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "recovery_kit.py"
TEMPLATE = ROOT / "templates" / "recovery-kit.md"
SEED = ROOT / "seed" / "seed.py"
SIGNING_KEY = ROOT / "installer" / "odile-signing-key.pub"
DECIDED = ROOT / "docs" / "product" / "CLAIMS-RECUT-STRICT.md"

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import claims_lint  # noqa: E402
from odilectl.config import Config  # noqa: E402

SLUG = "odl-7f3a2c"
IPV4 = "203.0.113.120"
INSTANCE = "srv-fixture0001"
TOKEN = "0123456789abcdef" * 4
EXPIRES = "2026-11-15T22:30:00Z"
WRITTEN = "2026-11-08T22:30:00Z"
MEASUREMENT = "ab" * 48
REPO = "sftp:odile-backup@backup.odile.fyi:/tenants/odl-7f3a2c"
URL = f"https://{SLUG}.odile.fyi"
FIRST_VISIT = f"{URL}/first-visit"
ACCOUNT = "https://test.odile.fyi"

# The sentence a reader acts on when the renderer is given no account host. The
# one-code facts replaced the ODL-579 rotation wording (ODL-926): the account
# password is reset at the account site, not on the machine, and the machine's
# reset is the one that needs the machine up.
FALLBACK_PARAGRAPH = (
    "Your account password is not reset on the machine. It resets at your account\n"
    "site — the site in your invitation email. Open that site and add /forgot. That\n"
    "reset works while the machine is down; the machine's reset needs the machine up.\n"
)

# The four code rows of ODL-926, quoted verbatim from docs/CLAIMS.md and named
# so a test can say which row it pins: D-6 (`:61`, the replace clause) retires
# the A9 "the agent can show it again" tail; D-20 (`:62`) is the stability rule;
# D-4 (`:111`) is one code on two surfaces; D-5 (`:112`) is the lost-code answer.
D6 = (
    "A recovery code and a recovery file are the way back: the code resets your "
    "sign-in, the file opens your backup. Your first visit gives you both; keep "
    "them off the machine. While the machine is still running, the agent can "
    "replace the code and show you the new one."
)
D20 = (
    "Your recovery code works at both places until you replace it; using it does "
    "not spend it."
)
D4 = (
    "One recovery code resets both: your account password at the account page and "
    "your machine's sign-in on the machine. There is no emailed reset."
)
D5 = (
    "If you lose your recovery code there is no other way back: you start a new "
    "account, and a backup is the only way to bring the files."
)

# Every one of these is a deck row (`docs/CLAIMS.md` §1 Access and §1 First run)
# or a decided v3.9 row that deck keeps. The kit may not coin a trust sentence of
# its own.
DECK_SENTENCES = (
    D6,
    "Your Odile password opens your machine and your account page. You chose it "
    "when you registered. It is the only app-layer gate: no 2FA in alpha.0.",
    "The disk unlocks only on the boot you verify. Change the image, and it "
    "stays locked.",
    "After a reboot, it starts itself. The seal is the key.",
    "Check the machine before you trust it: the verify page shows PASS or FAIL.",
    "The public key is in your recovery kit and at "
    "https://odile.fyi/odile-signing-key.pub.",
    "This is an operator signature, not a hardware measurement",
    "A grant is a handover, not a peek.",
    "Expiry and revoke block new logins. An open session keeps running until it "
    "exits or you kill it.",
    "The encrypted backup is kept 30 days after cancellation, then deleted. "
    "Odile can delete it, not read it.",
    D20,
    D4,
    D5,
)

# The rotation wordings ODL-926 retires (DECISION-one-recovery-code §2.2 and §5,
# and the stale-copy census that greps them). Quoted here only as the negative
# pin's evidence; none may appear in a rendered kit.
RETIRED_ROTATION_WORDINGS = (
    "the agent can show it again",
    "using it there rotates it too",
    "After a reset the old code stops working",
    "a new one is shown",
)

# The v3.7 Model A access sentences the kit used to carry. None may survive.
V37_SENTENCES = (
    "The only SSH key is yours.",
    "The boot key opens the machine at boot.",
    "You set the sign-in during the install.",
    "Your disk passphrase is yours. We never see it, and we hold no copy.",
    "The passphrase is typed over SSH and stays on the machine.",
    "Copy it off the machine, then shred the copy.",
    "Your key gets you in, and your passphrase unlocks the disk.",
)

# No instruction may carry one of these; the strict bar has no terminal step.
TERMINAL_WORDS = ("ssh", "scp", "ssh-keygen", "passphrase", "installer",
                  "shred", "~/.ssh")

SEED_CONFIG = {
    "near_api_key": "near-test-key-0001",
    "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
    "support_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEST support@odile.fyi",
    "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/{slug}",
    "hermes_version": "2026.11.1",
    "mail_relay": {
        "host": "smtp.mail.odile.fyi",
        "port": 587,
        "username": "{slug}@mail.odile.fyi",
        "secret": "relay-credential-0001",
        # ODL-469: a seed without the relay's PEM cannot send the brief.
        "tls_ca": ("-----BEGIN CERTIFICATE-----\n"
                   "MIIBhTCCASugAwIBAgIURECIPECA000000000000000000\n"
                   "-----END CERTIFICATE-----\n"),
    },
}


def flat(text: str) -> str:
    """The text in one comparison space: line wrapping is not a copy change."""
    return re.sub(r"\s+", " ", text).strip()


def deck_flat(text: str) -> str:
    """Deck prose without its markdown code ticks (a tick is not a sentence)."""
    return flat(text.replace("`", ""))


def load_script():
    """The generator module, so a test can render a mutated template."""
    spec = importlib.util.spec_from_file_location("recovery_kit", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load scripts/recovery_kit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_values() -> dict[str, object]:
    """The render values, built by the generator's own validator."""
    args = argparse.Namespace(
        slug=SLUG, ipv4=IPV4, instance_id=INSTANCE, token=TOKEN,
        expires_at=EXPIRES, release="2026.11.1", vcpus=4,
        measurement=MEASUREMENT, first_visit_url=FIRST_VISIT,
        signing_key=str(SIGNING_KEY), backup_repo_url=REPO, written_at=WRITTEN,
    )
    return load_script().build_values(args)


def render(*extra: str, **overrides: str | None) -> subprocess.CompletedProcess[str]:
    values: dict[str, str | None] = {
        "slug": SLUG,
        "ipv4": IPV4,
        "instance-id": INSTANCE,
        "token": TOKEN,
        "expires-at": EXPIRES,
        "written-at": WRITTEN,
        "measurement": MEASUREMENT,
        "backup-repo-url": REPO,
    }
    values.update({key.replace("_", "-"): value for key, value in overrides.items()})
    command = [sys.executable, str(SCRIPT), "render"]
    for key, value in values.items():
        if value is None:
            continue
        command.extend([f"--{key}", value])
    command.extend(extra)
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


def kit_text(**overrides: str | None) -> str:
    result = render(**overrides)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


def visit_url(text: str) -> str:
    """The kit's first sign-in URL, as the user reads it (tokenless)."""
    match = re.search(r"Open (\S+/first-visit)\.", flat(text))
    if match is None:
        raise AssertionError("the kit has no 'Open <url>/first-visit.' sentence")
    return match.group(1)


class RenderTests(unittest.TestCase):
    def test_the_kit_carries_the_machine_values(self) -> None:
        text = kit_text()
        for value in (SLUG, IPV4, INSTANCE, MEASUREMENT, REPO):
            self.assertIn(value, text)
        self.assertRegex(text, rf"(?m)^URL:\s+{re.escape(URL)}$")
        self.assertRegex(text, r"(?m)^Release:\s+2026\.11\.1$")
        self.assertRegex(text, r"(?m)^vCPUs:\s+4$")
        self.assertRegex(text, rf"(?m)^Machine ID:\s+{re.escape(INSTANCE)}$")
        self.assertRegex(text, rf"(?m)^Measurement:\s+{MEASUREMENT}$")

    def test_the_first_sign_in_line_is_the_tokenless_page(self) -> None:
        """The one action is the tokenless page (B-once, ODL-925)."""
        text = kit_text()
        self.assertEqual(visit_url(text), FIRST_VISIT)
        self.assertNotIn("?t=", text)
        self.assertEqual(text.count(TOKEN), 0, "the setup token is not a kit value")
        self.assertNotIn("setup_token", text)
        self.assertNotIn("recovery-kit.json", text)

    def test_the_kit_has_no_terminal_key_or_passphrase_instruction(self) -> None:
        text = kit_text()
        folded = text.lower()
        for word in TERMINAL_WORDS:
            self.assertNotIn(word, folded, f"the kit still says {word!r}")
        self.assertNotIn("root@", text)
        self.assertNotIn("-----BEGIN OPENSSH", text)
        self.assertNotIn("PRIVATE KEY", text)
        for sentence in V37_SENTENCES:
            self.assertNotIn(flat(sentence), flat(text), f"v3.7 sentence survived: {sentence!r}")

    def test_the_reader_checks_before_signing_in(self) -> None:
        """Check then sign in: /verify and PASS come before the first-sign-in page."""
        text = kit_text()
        check = text.index("\nBefore you sign in\n")
        visit = text.index("\nThe first sign-in\n")
        self.assertLess(check, visit, "the kit must read check -> sign in")
        self.assertLess(text.index(f"{URL}/verify"), visit)

    def test_the_masthead_is_sentence_case(self) -> None:
        text = kit_text()
        self.assertEqual(text.splitlines()[0], f"Odile recovery kit — {SLUG}")
        self.assertNotIn("ODILE RECOVERY KIT", text)

    def test_the_token_expiry_is_gone(self) -> None:
        """B-once: the kit prints no token, so it prints no token expiry."""
        text = kit_text()
        self.assertNotIn("Token expires", text)
        self.assertNotIn(EXPIRES, text)
        self.assertNotIn("The link expires", flat(text))

    def test_the_sign_in_is_the_password_chosen_at_registration(self) -> None:
        text = flat(kit_text())
        self.assertIn(
            "Sign in with the password you chose when you registered.", text)
        self.assertIn(
            "The first sign-in shows your recovery code once and offers your "
            "recovery file.", text)
        self.assertNotIn("at least 16 characters", text)
        self.assertIn(
            "Download your copy on the first visit and keep it off the machine: "
            "it is your only copy of the backup password.", text)
        self.assertNotIn("The file stays on the machine", text)

    def test_the_action_names_the_page_and_carries_d1(self) -> None:
        text = flat(kit_text())
        self.assertIn(f"Open {FIRST_VISIT}.", text)
        self.assertIn(flat(DECK_SENTENCES[1]), text, "D-1 is the page's footer")

    def test_the_two_ways_back_are_printed(self) -> None:
        text = flat(kit_text())
        self.assertIn(flat(D6), text, "D-6 is the recovery story")
        self.assertIn(f"{URL}/first-visit/reset", text, "the recovery code resets the sign-in")
        self.assertIn(f"odile-recovery-{SLUG}.txt", text, "the downloaded recovery file is named")
        self.assertIn(f"odile-{SLUG}-recovery-kit.txt", text, "this kit is named separately")
        self.assertIn("backup password", text)
        self.assertIn("certificate fingerprint", text)

    def test_the_retired_rotation_wording_is_gone(self) -> None:
        """NEGATIVE PIN (ODL-926): the code is stable, so no rotation copy survives.

        The four wordings in `RETIRED_ROTATION_WORDINGS` are the ones the
        one-code decision retires; the kit said three of them before this cut.
        A render that prints one again is the regression this test exists for,
        in both the fallback and the `--account-host` branch.
        """
        for label, text in (("fallback", kit_text()),
                            ("account slot", kit_text(account_host=ACCOUNT))):
            with self.subTest(label):
                rendered = flat(text)
                for phrase in RETIRED_ROTATION_WORDINGS:
                    self.assertNotIn(phrase, rendered,
                                     f"retired rotation wording survived: {phrase!r}")
                self.assertIn(flat(D20), rendered, "D-20 is the stability rule")
                self.assertIn(flat(D4), rendered, "D-4 is the one-code rule")

    def test_without_the_account_slot_the_sentence_is_byte_identical(self) -> None:
        """No slot: the paragraph the reader acts on is exactly FALLBACK_PARAGRAPH."""
        text = kit_text()
        self.assertIn(FALLBACK_PARAGRAPH, text)
        self.assertNotIn("{{", text)
        self.assertEqual(kit_text(account_host=""), text,
                         "an empty --account-host is the unset slot, not a different kit")

    def test_the_account_slot_prints_an_address_the_reader_can_type(self) -> None:
        """The action sentence carries the literal URL, and nothing else moves."""
        fallback = kit_text()
        text = kit_text(account_host=ACCOUNT)
        self.assertIn(
            "Your account password is not reset on the machine. It resets at\n"
            f"{ACCOUNT}/forgot. That reset works while the machine is down;\n"
            "the machine's reset needs the machine up.\n",
            text)
        self.assertNotIn("{{", text, "no placeholder survives a render")
        self.assertNotIn("invitation email", text)
        self.assertEqual(text.count(f"{ACCOUNT}/forgot"), 1)
        self.assertNotIn(f"{ACCOUNT}//forgot", text)
        # Only the sentence's own lines move; the rest of the kit is byte-identical.
        self.assertEqual(len(fallback.splitlines()), len(text.splitlines()))
        differing = [(a, b) for a, b in zip(fallback.splitlines(), text.splitlines()) if a != b]
        self.assertEqual(len(differing), 3, differing)

    def test_a_trailing_slash_on_the_account_host_does_not_double_the_path(self) -> None:
        text = kit_text(account_host=f"{ACCOUNT}/")
        self.assertIn(f"{ACCOUNT}/forgot.", text)
        self.assertNotIn(f"{ACCOUNT}//forgot", text)

    def test_a_pasted_reset_url_renders_exactly_one_forgot(self) -> None:
        """`<host>/forgot` is the value the decision doc prints; it stands for the host.

        Measured on t_cc6cb872: the flag trimmed only a trailing `/`, so the
        natural paste from `DECISION-password-reset.md` rendered
        `https://test.odile.fyi/forgot/forgot` at rc=0, with no warning.
        """
        expected = kit_text(account_host=ACCOUNT)
        for value in (f"{ACCOUNT}/forgot", f"{ACCOUNT}/forgot/"):
            with self.subTest(value):
                text = kit_text(account_host=value)
                self.assertNotIn(f"{ACCOUNT}/forgot/forgot", text)
                self.assertEqual(text.count(f"{ACCOUNT}/forgot"), 1)
                self.assertEqual(
                    text, expected,
                    "a pasted reset URL renders the host it starts with, byte for byte")

    def test_the_config_example_names_the_slot_and_uncomments_to_a_working_host(self) -> None:
        """The example is where an operator learns the key (the second t_cc6cb872 nit).

        `odilectl/config.py` treats `#` as an ordinary character in a value, so
        an inline comment on the key line would land in the value the moment the
        line is uncommented. Pin the whole discovery path: uncomment the
        example's own line, load it through the real loader, render with it.
        """
        lines = (ROOT / "odilectl" / "config.example.yaml").read_text(
            encoding="utf-8").splitlines()
        slots = [line for line in lines if line.startswith("# account_host:")]
        self.assertEqual(len(slots), 1,
                         "the example names account_host once, as a comment")
        key, _, value = slots[0][len("# "):].partition(":")
        self.assertEqual(key, "account_host")
        value = value.strip()
        self.assertTrue(value.startswith("https://"), value)

        body = [line for line in lines
                if line.strip() and not line.lstrip().startswith("#")]
        body.append(f"{key}: {value}")
        with tempfile.TemporaryDirectory(prefix="odile-kit-config-") as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("\n".join(body) + "\n", encoding="utf-8")
            path.chmod(0o600)
            loaded = Config.load(path)
        self.assertEqual(loaded.get("account_host"), value,
                         "uncommenting the example's line yields the bare value")

        text = kit_text(account_host=loaded.get("account_host"))
        self.assertEqual(text.count(f"{value}/forgot"), 1)
        self.assertNotIn("the site in your invitation email", text)

    def test_a_bad_account_host_is_refused_naming_the_flag(self) -> None:
        for value in ("http://test.odile.fyi", "test.odile.fyi",
                      "https://test odile.fyi", "https://"):
            with self.subTest(value):
                result = render(account_host=value)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("recovery kit:", result.stderr)
                self.assertIn("--account-host", result.stderr)

    def test_the_kit_carries_both_public_halves_and_the_certificate(self) -> None:
        """ODL-514: a kit that names one key stops being an anchor the day the other signs."""
        text = kit_text()
        seed_pub_path = ROOT / "deploy" / "seed" / "seed-key.pub"
        cert_path = ROOT / "deploy" / "seed" / "seed-key.crt"
        seed_pem = seed_pub_path.read_text(encoding="utf-8").strip()
        certificate = cert_path.read_bytes()
        self.assertEqual(len(certificate), 64)
        self.assertIn(SIGNING_KEY.read_text(encoding="utf-8").strip(), text)
        self.assertIn(seed_pem, text)
        self.assertIn(base64.b64encode(certificate).decode("ascii"), text)
        for heading in ("Release signing key", "Seed signing key", "Seed certificate (base64)"):
            self.assertIn(heading, text)
        release_digest = hashlib.sha256(SIGNING_KEY.read_bytes()).hexdigest()
        seed_digest = hashlib.sha256(seed_pub_path.read_bytes()).hexdigest()
        self.assertNotEqual(release_digest, seed_digest, "the two keys are different keys")
        self.assertRegex(text, rf"(?m)^Signing key:\s+SHA-256 {release_digest}$")
        self.assertRegex(text, rf"(?m)^Seed signing key:\s+SHA-256 {seed_digest}$")
        self.assertRegex(
            text, rf"(?m)^Seed certificate:\s+SHA-256 {hashlib.sha256(certificate).hexdigest()}$")
        with tempfile.TemporaryDirectory() as directory:
            signature = Path(directory) / "cert"
            signature.write_bytes(certificate)
            proc = subprocess.run(
                ["openssl", "dgst", "-verify", str(SIGNING_KEY), "-signature", str(signature),
                 str(seed_pub_path)], capture_output=True, text=True)
            self.assertIn("Verified OK", proc.stdout,
                          "the certificate in the kit is the release key's signature over the seed key")

    def test_a_kit_with_a_certificate_for_another_seed_key_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "other.pub"
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out",
                            str(Path(directory) / "k.pem")], check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", str(Path(directory) / "k.pem"), "-pubout",
                            "-out", str(other)], check=True, capture_output=True)
            result = render(seed_signing_key=str(other))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("make seed-cert", result.stderr)

    def test_the_signing_key_is_the_published_one_and_never_a_secret(self) -> None:
        text = kit_text()
        digest = __import__("hashlib").sha256(SIGNING_KEY.read_bytes()).hexdigest()
        self.assertRegex(text, rf"(?m)^Signing key:\s+SHA-256 {digest}$")
        self.assertEqual(text.count(digest), 1, "one digest, on the fact line")
        self.assertIn(SIGNING_KEY.read_text(encoding="utf-8").strip(), text)
        self.assertIn("https://odile.fyi/odile-signing-key.pub", text)

    def test_every_trust_sentence_is_in_an_approved_deck(self) -> None:
        text = flat(kit_text())
        decks = deck_flat((ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")) + " "
        decks += deck_flat(DECIDED.read_text(encoding="utf-8"))
        # G13 renamed the deck's live version tag from V0.1 to alpha.0; the
        # decided v3.9 draft is the historical record and keeps the old tag,
        # while the kit quotes the live one. ODL-577 retired the kit's
        # first-visit and after-PASS rows from the deck (D-1, D-7), so until
        # ODL-925 re-cuts templates/recovery-kit.md the draft is their approval.
        decks = decks.replace("V0.1", "alpha.0")
        for sentence in DECK_SENTENCES:
            self.assertIn(flat(sentence), text)
            self.assertIn(flat(sentence).lower(), decks.lower(),
                          f"sentence not in docs/CLAIMS.md or the decided v3.9 rows: {sentence!r}")

    def test_the_rendered_kit_passes_the_claims_lint(self) -> None:
        text = kit_text()
        patterns = [(phrase, claims_lint.phrase_pattern(phrase))
                    for phrase in claims_lint.load_phrases(ROOT / "docs" / "CLAIMS.md")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kit.txt"
            path.write_text(text, encoding="utf-8")
            findings = claims_lint.scan(path, patterns)
        self.assertEqual(findings, [])

    def test_optional_fields_render_only_when_given(self) -> None:
        full = kit_text()
        self.assertIn(REPO, full)
        sparse = kit_text(backup_repo_url=None)
        self.assertNotIn(REPO, sparse)
        self.assertNotIn("Backup repository:", sparse)
        self.assertNotIn("{{", sparse)

    def test_the_render_is_deterministic(self) -> None:
        self.assertEqual(kit_text(), kit_text())
        later = kit_text(written_at="2026-11-09T00:00:00Z")
        differing = [(a, b) for a, b in zip(kit_text().splitlines(), later.splitlines()) if a != b]
        self.assertEqual(len(differing), 1, differing)
        self.assertIn("Generated:", differing[0][0])

    def test_bad_input_is_refused_with_a_reason(self) -> None:
        cases = {
            "uppercase token": {"token": "A" * 64},
            "short token": {"token": "a" * 63},
            "bad slug": {"slug": "Odile!"},
            "ipv6": {"ipv4": "2001:db8::1"},
            "bad instance": {"instance-id": "srv fixture"},
            "expired token": {"expires-at": "2026-11-01T00:00:00Z"},
            "bad release": {"release": "latest"},
            "zero vcpus": {"vcpus": "0"},
            "missing key": {"signing-key": "/nonexistent/odile.pub"},
            "uppercase measurement": {"measurement": "AB" * 48},
            "short measurement": {"measurement": "a" * 95},
            "visit url with a token": {
                "first-visit-url": f"{URL}/first-visit?t={TOKEN}"},
            "visit url is another page": {
                "first-visit-url": f"{URL}/verify"},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                result = render(**overrides)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("recovery kit:", result.stderr)
                self.assertNotEqual(result.stderr.strip(), "")

    def test_the_measurement_is_required(self) -> None:
        result = render(measurement=None)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--measurement", result.stderr)

    def test_template_blocks_and_leftovers(self) -> None:
        module = load_script()
        self.assertEqual(module.render("a{{#x}}b{{/x}}c", {"x": True}), "abc")
        self.assertEqual(module.render("a{{#x}}b{{/x}}c", {"x": False}), "ac")
        self.assertEqual(module.render("a{{^x}}b{{/x}}c", {"x": False}), "abc")
        self.assertEqual(module.render("a{{^x}}b{{/x}}c", {"x": True}), "ac")
        with self.assertRaises(module.BadInput):
            module.render("{{missing}}", {})

    def test_removing_the_visit_line_breaks_the_render(self) -> None:
        """The template's one action is not optional."""
        template = TEMPLATE.read_text(encoding="utf-8")
        module = load_script()
        mutant = template.replace("{{first_visit_url}}", "", 1)
        self.assertNotEqual(mutant, template)
        text = module.render(mutant, fixture_values())
        with self.assertRaises(AssertionError):
            visit_url(text)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_health(port: int, proc: subprocess.Popen, seconds: float = 10.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1).status == 200:
                return
        except OSError:
            time.sleep(0.05)
    output = b""
    if proc.poll() is not None:
        output = proc.communicate(timeout=5)[0] or b""
    raise RuntimeError("seed service did not start:\n" + output.decode("utf-8", "replace"))


class SeedOnceTests(unittest.TestCase):
    """The token in a rendered kit is the seed service's token, and it works once."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        directory = cls.tmp.name
        cls.db = os.path.join(directory, "waitlist.db")
        cls.key = os.path.join(directory, "seed-signing.pem")
        cls.config = os.path.join(directory, "seed-config.json")
        cls.release_key = os.path.join(directory, "release-signing.pem")
        cls.release_pub = cls.release_key + ".pub"
        cls.cert = os.path.join(directory, "seed-key.crt")
        cls.env = dict(
            os.environ,
            ODILE_DB=cls.db,
            ODILE_SEED_CONFIG=cls.config,
            ODILE_SEED_SIGNING_KEY=cls.key,
            ODILE_SEED_CERT=cls.cert,
            ODILE_SEED_RELEASE_PUBKEY=cls.release_pub,
        )
        keygen = cls.run_seed("keygen")
        if keygen.returncode != 0:
            raise RuntimeError(keygen.stderr)
        os.chmod(cls.key, 0o600)
        # ODL-514: the release key signs the certificate over the seed key.
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", cls.release_key],
                       check=True, capture_output=True)
        os.chmod(cls.release_key, 0o600)
        subprocess.run(["openssl", "pkey", "-in", cls.release_key, "-pubout",
                        "-out", cls.release_pub], check=True, capture_output=True)
        subprocess.run(["openssl", "dgst", "-sign", cls.release_key, "-out", cls.cert,
                        cls.key + ".pub"], check=True, capture_output=True)
        os.chmod(cls.cert, 0o644)
        with open(cls.config, "w", encoding="utf-8") as handle:
            json.dump(SEED_CONFIG, handle)
        os.chmod(cls.config, 0o600)
        init = cls.run_seed("init")
        if init.returncode != 0:
            raise RuntimeError(init.stderr)
        con = sqlite3.connect(cls.db)
        stamp = "2026-11-08T00:00:00Z"
        con.execute(
            "INSERT INTO tenants (slug, email, server_id, created_at) VALUES (?, ?, ?, ?)",
            (SLUG, "kit@example.com", INSTANCE, stamp),
        )
        con.commit()
        con.close()
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, str(SEED), "serve"],
            env=dict(cls.env, ODILE_SEED_PORT=str(cls.port), ODILE_SEED_BIND="127.0.0.1"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        wait_health(cls.port, cls.proc)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.communicate()
        cls.tmp.cleanup()

    @classmethod
    def run_seed(cls, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SEED), *args], env=cls.env,
            capture_output=True, text=True, timeout=30,
        )

    def issue(self) -> tuple[str, str]:
        result = self.run_seed("token", "issue", "--tenant", SLUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        token = re.search(r"^token:\s+([0-9a-f]{64})$", result.stdout, re.M)
        expires = re.search(r"^expires:\s+(\S+)$", result.stdout, re.M)
        self.assertIsNotNone(token, result.stdout)
        self.assertIsNotNone(expires, result.stdout)
        assert token is not None and expires is not None
        return token.group(1), expires.group(1)

    def exchange(self, token: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps({"token": token, "instance_id": INSTANCE}).encode("utf-8")
        connection.request("POST", "/seed", body=body,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_a_rendered_kit_prints_no_token_and_the_token_still_fetches_the_seed(self) -> None:
        token, expires = self.issue()
        text = kit_text(token=token, expires_at=expires)
        self.assertEqual(visit_url(text), FIRST_VISIT)
        self.assertNotIn("?t=", text)
        self.assertEqual(text.count(token), 0, "the kit never prints the setup token")
        first, body = self.exchange(token)
        self.assertEqual(first, 200, body[:300])
        seed = json.loads(body)
        self.assertEqual(seed["slug"], SLUG)
        self.assertEqual(seed["instance_id"], INSTANCE)
        second, body = self.exchange(token)
        self.assertEqual(second, 410, body[:300])


if __name__ == "__main__":
    unittest.main()
