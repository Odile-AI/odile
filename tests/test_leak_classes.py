#!/usr/bin/python3
"""The leak classes Odile actually needs, and the over-broad rules that are not.

Each class gets a planted positive that must be reported and a negative that must
not. The probe values are assembled at runtime, so this test file is itself clean
under the gate it exercises. The last test is the drill: a probe file turns the
gate red, and deleting it turns the same tree green.

Run directly (`/usr/bin/python3 tests/test_leak_classes.py`) or through the suite.
"""

from __future__ import annotations

import base64
import gzip
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import leak_scan  # noqa: E402

# Values assembled from parts: the source line must not itself be a probe.
CHIP = "0123456789abcdef" * 8
PARTUUID = "01234567-89ab-" + "cdef-0123-456789abcdef"
ZONE = "0123456789abcdef" * 2
FINGERPRINT = "SHA256:" + "A" * 43
MAC = "aa:bb:" + "cc:dd:ee:ff"
BROADCAST = "ff:ff:" + "ff:ff:ff:ff"
UNSET_MAC = "00:00:" + "00:00:00:00"
MACHINE_ID = "0123456789abcdef" * 2
SERIAL = "SN01" + "23456789"
INSTANCE = "baad" + "f00d"
HOST = "qwerty" + "ui"
SLUG = "odl-" + "1234ab"
# A bare id, and the shapes that share its look: a short commit id, a uuid
# fragment, a colour and a digest prefix.
BARE_ID = "31a6" + "3bbb"
BARE_HOST = "oqwa" + "clzm"
ACCOUNT = "lkbx" + "u6i9"
COMMIT = "feed" + "c0de"
COLOUR = "94ee" + "0477"
UUID = "01234567-89ab-" + "cdef-0123-456789abcdef"
DIGEST = "sha256:" + BARE_ID + "0" * 56
PASSPHRASE = "correct-horse-" + "battery-staple"
PASSPHRASE_WORDS = "Correct Horse " + "Battery Staple"
PROSE = "correct horse, " + "battery staple."
MNEMONIC = ("abandon ability able about above absent " +
            "absorb abstract absurd abuse access accident")
SECRET_KEY = "this-is-a-long-" + "key-with-no-digits"
ROOT_CHECKOUT = "/root/" + "operator-checkout"
MAC_HOME = "/Users/" + "alice" + "/notes"
WIN_HOME = "C:\\Users" + "\\" + "alice" + "\\" + "notes"
PHONE = "(415) " + "555-2671"
PHONE_DASHED = "415-" + "555-2671"
DASHED_HOST = "23-155-" + "44-139.sslip.io"
PUBLIC_IP = "45.33.32." + "156"
EMAIL = "person@" + "fastmail.com"
EMAIL_REAL = "real.person@" + "fastmail.com"
TOKEN_VALUE = "Ab3dEf6hIj9k" + "Lm2nOp5qRs8tUv1wXy4zA7bCd0"


def rules(line: str) -> list[str]:
    return [rule for rule, _value, _note in leak_scan.text_candidates(line, 1)]


HWID_OID = bytes.fromhex("2b060104019c780104")


def _tlv(tag: int, value: bytes) -> bytes:
    length = len(value)
    if length < 0x80:
        header = bytes([length])
    else:
        body = length.to_bytes((length.bit_length() + 7) // 8, "big")
        header = bytes([0x80 | len(body)]) + body
    return bytes([tag]) + header + value


def fake_vcek(hwid: bytes, serial: bytes = b"\x01" * 16) -> bytes:
    """A DER certificate skeleton carrying just the fields the class reads."""
    fields = (_tlv(0x02, serial) + _tlv(0x06, HWID_OID) + _tlv(0x04, hwid))
    return _tlv(0x30, _tlv(0x30, fields))


def pem(der: bytes, label: str = "CERTIFICATE") -> str:
    body = base64.b64encode(der).decode("ascii")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (
        label, "\n".join(lines), label)


class MachineIdentity(unittest.TestCase):
    def test_a_chip_id_is_reported(self):
        self.assertIn("machine-identity", rules(f"chip {CHIP}"))

    def test_a_partuuid_is_reported(self):
        self.assertIn("machine-identity", rules(f"root=PARTUUID={PARTUUID} ro"))

    def test_a_machine_id_is_reported(self):
        self.assertIn("machine-identity", rules(f"machine-id: {MACHINE_ID}"))

    def test_a_mac_is_reported(self):
        self.assertIn("machine-identity", rules(f"link {MAC} up"))

    def test_the_broadcast_beside_a_hardware_address_is_not_a_machine(self):
        # `ip addr` prints the broadcast address on the hardware-address line;
        # only the hardware address itself names this machine.
        found = [value for rule, value, _note
                 in leak_scan.text_candidates(f"link/ether {MAC} brd {BROADCAST}", 1)]
        self.assertEqual(found, [MAC])

    def test_a_broadcast_or_unset_mac_is_not_a_machine(self):
        self.assertNotIn("machine-identity", rules(f"brd {BROADCAST}"))
        self.assertNotIn("machine-identity", rules(f"link/ether {UNSET_MAC}"))

    def test_a_hardware_serial_is_reported(self):
        self.assertIn("machine-identity", rules(f"Chassis Serial: {SERIAL}"))

    def test_a_measurement_is_not_a_chip_id(self):
        self.assertNotIn("machine-identity", rules("measurement " + "ab" * 48))

    def test_a_plain_serial_variable_is_not_a_serial_number(self):
        self.assertNotIn("machine-identity", rules('serial = pathlib.Path(tmp)'))

    def test_a_sha512_digest_is_not_a_chip_id(self):
        self.assertNotIn("machine-identity", rules("sha512 " + "ab" * 64))

    def test_a_report_chip_id_is_reported(self):
        raw = bytearray(1184)
        raw[0:4] = (5).to_bytes(4, "little")
        raw[0x90:0xC0] = b"\x11" * 48
        raw[0x1A0:0x1E0] = bytes.fromhex(CHIP)
        self.assertIn("machine-identity", [rule for rule, _v, _n
                                           in leak_scan.report_identifiers(bytes(raw))])

    def test_a_synthetic_report_chip_id_is_not_a_machine(self):
        raw = bytearray(1184)
        raw[0:4] = (5).to_bytes(4, "little")
        raw[0x90:0xC0] = b"\x11" * 48
        raw[0x1A0:0x1E0] = (leak_scan.SYNTHETIC_CHIP_PREFIX
                            + b"SEV-SNP-CHIP-ID-0001").ljust(64, b"\x00")
        self.assertEqual(leak_scan.report_identifiers(bytes(raw)), [])

    def test_a_real_chip_id_after_the_synthetic_marker_is_reported(self):
        # The marker alone must not blind the field: a silicon id spliced
        # after it is still a machine.
        raw = bytearray(1184)
        raw[0:4] = (5).to_bytes(4, "little")
        raw[0x90:0xC0] = b"\x11" * 48
        raw[0x1A0:0x1E0] = (leak_scan.SYNTHETIC_CHIP_PREFIX
                            + bytes.fromhex(CHIP)[:48]).ljust(64, b"\x00")
        self.assertIn("machine-identity",
                      [rule for rule, _v, _n
                       in leak_scan.report_identifiers(bytes(raw))])

    def test_a_vcek_hwid_is_reported(self):
        cert = fake_vcek(bytes.fromhex(CHIP))
        found = [rule for rule, _v, _n in leak_scan.der_identifiers(cert)]
        self.assertIn("machine-identity", found)

    def test_a_synthetic_vcek_hwid_is_not_a_machine(self):
        hwid = (leak_scan.SYNTHETIC_CHIP_PREFIX + b"SEV-SNP-CHIP-ID-0001") \
            .ljust(64, b"\x00")
        self.assertEqual(leak_scan.der_identifiers(fake_vcek(hwid)), [])

    def test_a_certificate_without_a_hwid_is_not_a_vcek(self):
        # Every certificate has a serial; only a VCEK's serial identifies a
        # chip, so a bare serial must not become a machine identity.
        cert = _tlv(0x30, _tlv(0x30, _tlv(0x02, b"\x01" * 16)))
        self.assertEqual(leak_scan.der_identifiers(cert), [])

    def test_a_pem_certificate_is_read(self):
        text = pem(fake_vcek(bytes.fromhex(CHIP)))
        found = [rule for rule, _l, _v, _n in leak_scan.scan_text(text)]
        self.assertIn("machine-identity", found)

    def test_a_synthetic_pem_certificate_is_not_a_machine(self):
        hwid = (leak_scan.SYNTHETIC_CHIP_PREFIX + b"SEV-SNP-CHIP-ID-0001") \
            .ljust(64, b"\x00")
        text = pem(fake_vcek(hwid))
        found = [rule for rule, _l, _v, _n in leak_scan.scan_text(text)]
        self.assertNotIn("machine-identity", found)


class VendorResourceIds(unittest.TestCase):
    def test_an_instance_id_is_reported(self):
        self.assertIn("vendor-resource-id", rules(f"bb.iid={INSTANCE}"))

    def test_a_host_name_is_reported(self):
        self.assertIn("vendor-resource-id", rules(f"bb.host={HOST}"))

    def test_a_tenant_slug_is_reported(self):
        self.assertIn("vendor-resource-id", rules(f"tenant {SLUG}"))

    def test_a_commit_sha_is_not_an_instance_id(self):
        self.assertNotIn("vendor-resource-id", rules("commit deadbeef fixes it"))

    def test_a_fixture_host_is_not_a_provider_host(self):
        self.assertNotIn("vendor-resource-id", rules("bb.host=hostfake"))

    def test_a_public_stand_in_slug_is_not_a_tenant(self):
        self.assertNotIn("vendor-resource-id", rules("tenant odl-fake01"))

    def test_a_bare_instance_id_in_a_code_span_is_reported(self):
        # No `bb.iid=` / `server_id=` label: the code span is the position.
        self.assertIn("vendor-resource-id", rules(f"the machine is `{BARE_ID}`"))

    def test_a_bare_instance_id_in_a_query_parameter_is_reported(self):
        self.assertIn("vendor-resource-id", rules(f"?action=server&id={BARE_ID}"))

    def test_a_bare_instance_id_after_an_instance_cue_is_reported(self):
        self.assertIn("vendor-resource-id", rules(f"reinstalled instance {BARE_ID}"))

    def test_a_bare_host_name_in_a_hostname_is_reported(self):
        self.assertIn("vendor-resource-id", rules(f"the PTR was `{BARE_HOST}.local`"))

    def test_a_bare_host_name_in_a_mailbox_realm_is_reported(self):
        self.assertIn("vendor-resource-id", rules(f"stored as user@{BARE_HOST}"))

    def test_an_account_id_of_another_shape_is_reported(self):
        # A Servury account id is 8 lowercase alphanumerics, not hex: the
        # labeled rule's value shape must reach an `account_id=` value that
        # carries a digit but is not hex.
        self.assertIn("vendor-resource-id", rules(f"account_id={ACCOUNT}"))
        self.assertIn("vendor-resource-id", rules(f"account={ACCOUNT}"))

    def test_an_eight_letter_word_is_not_an_account_id(self):
        self.assertNotIn("vendor-resource-id", rules("account=username"))

    def test_a_commit_sha_is_not_a_bare_id(self):
        # A code span is also how a short commit id is written; the clause
        # (`merged`, `commit`, `HEAD`) says which one it is.
        self.assertNotIn("vendor-resource-id", rules(f"merged pre-flip at `{COMMIT}`"))
        self.assertNotIn("vendor-resource-id", rules(f"HEAD is `{COMMIT}` now"))

    def test_a_uuid_fragment_is_not_a_bare_id(self):
        self.assertNotIn("vendor-resource-id", rules(f"uuid {UUID}"))

    def test_a_digest_prefix_is_not_a_bare_id(self):
        self.assertNotIn("vendor-resource-id", rules(f"checksum {DIGEST}"))

    def test_a_colour_is_not_a_bare_id(self):
        self.assertNotIn("vendor-resource-id", rules(f"the tint is #{COLOUR}"))


class DnsResourceIds(unittest.TestCase):
    def test_a_zone_id_is_reported(self):
        self.assertIn("dns-resource-id", rules(f'ZONE_ID = "{ZONE}"'))

    def test_a_record_id_is_reported(self):
        self.assertIn("dns-resource-id", rules(f'record_id: "{ZONE}"'))

    def test_an_md5_is_not_a_zone_id(self):
        self.assertNotIn("dns-resource-id", rules(f'md5 = "{ZONE}"'))


class Fingerprints(unittest.TestCase):
    def test_a_sha256_fingerprint_is_reported_on_a_sha256_line(self):
        self.assertIn("ssh-fingerprint", rules(f"host key {FINGERPRINT} (sha256)"))

    def test_a_placeholder_fingerprint_is_not_reported(self):
        self.assertNotIn("ssh-fingerprint", rules("host key SHA256:<fingerprint>"))


class Credentials(unittest.TestCase):
    def test_a_passphrase_without_a_digit_is_reported(self):
        self.assertIn("credential-assignment", rules(f"LUKS_PASSPHRASE={PASSPHRASE}"))

    def test_a_capitalised_passphrase_is_reported(self):
        self.assertIn("credential-assignment",
                      rules(f'LUKS_PASSPHRASE="{PASSPHRASE_WORDS}"'))

    def test_a_prose_phrase_with_punctuation_is_not_a_passphrase(self):
        self.assertNotIn("credential-assignment",
                         rules(f'LUKS_PASSPHRASE="{PROSE}"'))

    def test_a_mnemonic_is_reported(self):
        self.assertIn("credential-assignment", rules(f'MNEMONIC="{MNEMONIC}"'))

    def test_a_secret_key_is_reported(self):
        self.assertIn("credential-assignment", rules(f"SECRET_KEY={SECRET_KEY}"))

    def test_a_placeholder_passphrase_is_not_reported(self):
        self.assertNotIn("credential-assignment", rules("LUKS_PASSPHRASE=<redacted>"))

    def test_a_dotted_expression_is_not_a_value(self):
        self.assertNotIn("credential-assignment", rules("CREDENTIAL = CREDENTIAL_FILE.read_text()"))

    def test_a_table_credential_is_reported(self):
        self.assertIn("credential-literal",
                      rules("| Password | `odile-demo-kestrel-2099` |"))

    def test_a_table_placeholder_is_not_reported(self):
        self.assertNotIn("credential-literal", rules("| Password | `<redacted>` |"))


class HomePaths(unittest.TestCase):
    def test_a_root_checkout_is_reported(self):
        self.assertIn("home-path", rules(f"cd {ROOT_CHECKOUT}"))

    def test_the_product_root_paths_are_not_reported(self):
        self.assertNotIn("home-path", rules("cat /root/.odile/config.json"))

    def test_a_macos_home_is_reported(self):
        self.assertIn("home-path", rules(f"open {MAC_HOME}"))

    def test_a_windows_home_is_reported(self):
        self.assertIn("home-path", rules("dir " + WIN_HOME))

    def test_a_shared_windows_home_is_not_reported(self):
        self.assertNotIn("home-path", rules("dir " + "C:\\Users" + "\\" + "Public" + "\\"))


class Phones(unittest.TestCase):
    def test_a_national_number_is_reported(self):
        self.assertIn("phone", rules(f"call {PHONE}"))

    def test_a_dashed_national_number_is_reported(self):
        self.assertIn("phone", rules(f"call {PHONE_DASHED}"))

    def test_an_iso_date_is_not_a_phone(self):
        self.assertNotIn("phone", rules("on 2026-11-15"))


class Addresses(unittest.TestCase):
    def test_an_address_dashed_into_a_hostname_is_reported(self):
        self.assertIn("public-ip", rules(f"https://{DASHED_HOST}/"))

    def test_a_cgnat_address_is_not_public(self):
        self.assertNotIn("public-ip", rules("tailscale 100.64.1.2"))

    def test_a_benchmark_address_is_not_public(self):
        self.assertNotIn("public-ip", rules("bench 198.18.0.1"))

    def test_a_version_quadruple_is_not_an_address(self):
        self.assertNotIn("public-ip", rules("version 1.2.3.4"))

    def test_a_quadruple_at_the_version_keyword_is_not_an_address(self):
        # The keyword may be followed by a copula, not by more of the sentence.
        self.assertNotIn("public-ip", rules("the release is 1.2.3.4"))

    def test_a_v_prefixed_version_is_not_an_address(self):
        self.assertNotIn("public-ip", rules("odile image v1.2.3.4"))

    def test_an_address_after_a_verbose_flag_is_reported(self):
        # `-v` is a flag, not the version keyword: the address must still fire.
        self.assertIn("public-ip", rules(f"curl -v {PUBLIC_IP}"))

    def test_an_address_on_a_line_naming_a_release_is_reported(self):
        # The version word used to blind the whole line: an address anywhere on
        # a release-notes line passed (ODL-906-F3 round 2).
        self.assertIn("public-ip", rules(f"release 2026.11.1 the testing host is {PUBLIC_IP}"))

    def test_an_address_after_a_version_on_the_same_line_is_reported(self):
        self.assertIn("public-ip", rules(f"release 1.2.3.4 and {PUBLIC_IP}"))

    def test_a_data_uri_no_longer_hides_the_line(self):
        found = rules(f"font url(data:image/png;base64,iVBORw0KGgo=) {PUBLIC_IP} {EMAIL}")
        self.assertIn("public-ip", found)
        self.assertIn("email", found)


class HighEntropyPrefix(unittest.TestCase):
    def test_the_name_prefix_is_stripped_before_entropy(self):
        found = leak_scan.text_candidates("PREFIX_VALUE=" + TOKEN_VALUE, 1)
        self.assertEqual([item[1] for item in found if item[0] == "high-entropy"], [TOKEN_VALUE])


class OpaquePayloads(unittest.TestCase):
    def test_a_utf16_mailbox_is_reported(self):
        found = leak_scan.scan_bytes(("mail " + EMAIL + "\n").encode("utf-16-le"))
        self.assertTrue(any(rule == "email" for rule, _line, _value, _note in found))

    def test_a_long_base64_mailbox_is_reported(self):
        payload = (EMAIL + " " + "x" * 120).encode("utf-8")
        line = "config " + base64.b64encode(payload).decode("ascii")
        found = leak_scan.scan_text(line)
        self.assertTrue(any(rule == "email" for rule, _line, _value, _note in found))

    def test_a_short_base64_run_is_not_decoded(self):
        line = "config " + base64.b64encode(EMAIL.encode("ascii")).decode("ascii")
        found = leak_scan.scan_text(line)
        self.assertFalse(any(rule == "email" for rule, _line, _value, _note in found))

    def test_a_tar_member_is_scanned(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            data = f"host {PUBLIC_IP}\nmail {EMAIL}\n".encode("utf-8")
            info = tarfile.TarInfo("member.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        found = leak_scan.scan_archive("bundle.tar.gz", buffer.getvalue())
        paths = {path for path, _candidates, _text in found}
        self.assertIn("bundle.tar.gz!member.txt", paths)
        rules_found = {rule for _path, candidates, _text in found
                       for rule, _line, _value, _note in candidates}
        self.assertIn("public-ip", rules_found)
        self.assertIn("email", rules_found)

    def test_a_benign_tar_member_is_clean(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            data = b"nothing to see here\n"
            info = tarfile.TarInfo("member.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        found = leak_scan.scan_archive("bundle.tar.gz", buffer.getvalue())
        self.assertEqual([(path, candidates) for path, candidates, _text in found],
                         [("bundle.tar.gz!member.txt", [])])


class ArchiveFormats(unittest.TestCase):
    """Every archive format the class names is read, and a clean member is clean.

    `scan_archive` also returns the member's own text view (so the forbid list
    can run inside an archive), so these tests read the path and the candidates
    by position and leave the rest of the tuple to the scanner.
    """

    @staticmethod
    def paths(found) -> list[str]:
        return [member[0] for member in found]

    @staticmethod
    def candidates(found, index: int = 0) -> list[tuple[str, int, str, str]]:
        return found[index][1]

    def test_a_zip_member_is_scanned(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("member.txt", f"host {PUBLIC_IP}\nmail {EMAIL}\n")
        found = leak_scan.scan_archive("bundle.zip", buffer.getvalue())
        self.assertEqual(self.paths(found), ["bundle.zip!member.txt"])
        rules_found = {rule for rule, _line, _value, _note in self.candidates(found)}
        self.assertIn("public-ip", rules_found)
        self.assertIn("email", rules_found)

    def test_a_benign_zip_member_is_clean(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("member.txt", "nothing to see here\n")
        found = leak_scan.scan_archive("bundle.zip", buffer.getvalue())
        self.assertEqual(self.paths(found), ["bundle.zip!member.txt"])
        self.assertEqual(self.candidates(found), [])

    def test_a_plain_gzip_member_is_scanned(self):
        # A gzip that is not a tar: `scan_archive` falls back to decompressing
        # it, and the member is scanned at the path it would have had.
        found = leak_scan.scan_archive(
            "notes.txt.gz", gzip.compress(f"host {PUBLIC_IP}\nmail {EMAIL}\n".encode("utf-8")))
        self.assertEqual(self.paths(found), ["notes.txt.gz!notes.txt"])
        rules_found = {rule for rule, _line, _value, _note in self.candidates(found)}
        self.assertIn("public-ip", rules_found)
        self.assertIn("email", rules_found)

    def test_a_benign_plain_gzip_member_is_clean(self):
        found = leak_scan.scan_archive("notes.txt.gz", gzip.compress(b"nothing to see here\n"))
        self.assertEqual(self.paths(found), ["notes.txt.gz!notes.txt"])
        self.assertEqual(self.candidates(found), [])


def git_env() -> dict[str, str]:
    """A clean environment: a fixture repo must never inherit GIT_DIR.

    `.githooks/pre-commit` runs the suite with GIT_DIR / GIT_INDEX_FILE
    pointing at the real repository, and in a linked worktree — every card
    worktree — both are absolute. An inherited GIT_DIR makes `git init` in the
    fixture commit *into* the caller's worktree and the scanner's `git ls-files`
    walk read the outer tree instead of the fixture (the ODL-101 incident). Every
    child this file spawns gets this environment: the fixture's own git call and
    both scanner calls, because the scanner shells out to git itself.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env.update(
        {
            "GIT_AUTHOR_NAME": "Probe",
            "GIT_AUTHOR_EMAIL": "probe@example.test",
            "GIT_COMMITTER_NAME": "Probe",
            "GIT_COMMITTER_EMAIL": "probe@example.test",
        }
    )
    return env


def run_drill(root: Path) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Plant a probe, scan, delete it, scan again; the caller asserts the pair."""
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=git_env())
    probe = root / "probe.txt"
    probe.write_text(f"host {PUBLIC_IP}\nmail {EMAIL_REAL}\n", encoding="utf-8")
    scanner = str(ROOT / "tools" / "leak_scan.py")
    first = subprocess.run([sys.executable, scanner, "--tree", "--root", str(root)],
                           capture_output=True, text=True, timeout=60, env=git_env())
    probe.unlink()
    second = subprocess.run([sys.executable, scanner, "--tree", "--root", str(root)],
                            capture_output=True, text=True, timeout=60, env=git_env())
    return first, second


class Drill(unittest.TestCase):
    """The gate fails on a probe file, then passes once the probe is removed."""

    def test_fail_then_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = run_drill(Path(tmp))
            self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
            self.assertIn("FAIL:", first.stdout)
            self.assertIn("public-ip", first.stdout)
            self.assertIn("email", first.stdout)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("PASS:", second.stdout)

    def test_the_drill_survives_an_inherited_git_dir(self):
        # The pre-commit gate runs its test modules from the real checkout with
        # GIT_DIR / GIT_INDEX_FILE set; in a linked worktree both are absolute.
        # The child environment must be scrubbed, or `git init` and the
        # scanner's `git ls-files` read the outer tree and the probe is never
        # seen (the ODL-101 incident F1 fixed in tests/test_leak_scan.py).
        inherited = {
            "GIT_DIR": str(ROOT / ".git"),
            "GIT_INDEX_FILE": str(ROOT / ".git" / "index"),
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, inherited):
            first, second = run_drill(Path(tmp))
            self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
            self.assertIn("public-ip", first.stdout)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_a_bare_vendor_id_fails_the_gate_then_passes(self):
        # The card's acceptance: a bare id with no `server_id=` around it is a
        # finding, and the same tree passes once the probe is gone.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=git_env())
            probe = root / "probe.txt"
            probe.write_text(f"the machine is `{BARE_ID}`\n", encoding="utf-8")
            scanner = str(ROOT / "tools" / "leak_scan.py")
            first = subprocess.run([sys.executable, scanner, "--tree", "--root", str(root)],
                                   capture_output=True, text=True, timeout=60, env=git_env())
            probe.unlink()
            second = subprocess.run([sys.executable, scanner, "--tree", "--root", str(root)],
                                    capture_output=True, text=True, timeout=60, env=git_env())
            self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
            self.assertIn("FINDING vendor-resource-id", first.stdout)
            self.assertIn("bare instance id", first.stdout)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("PASS:", second.stdout)


if __name__ == "__main__":
    unittest.main()
