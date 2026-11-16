#!/usr/bin/python3
"""End-to-end checks for the waitlist flow.

Starts the real server against a throwaway database and speaks HTTP to it.
Standard library only, like the app.

    /usr/bin/python3 -m unittest discover -s tests -v
"""
import hashlib
import http.client
import io
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(ROOT, "deploy", "attest-refresh", "refresh.sh")

# A plausible cached report. The measurement is 48 bytes of hex; the timestamp
# has the one shape attest.py writes.
MEASUREMENT = "ab" * 48
FETCHED_AT = "2026-11-07T00:00:00Z"

# CLAIMS.md §3, in order; /verify may not reorder or reword them.
GAPS = (
    "The app layer is not hardware-measured yet.",
    "The console passes through the host.",
    "Odile keeps three powers.",
)

# The release surface is fixed by docs/runbooks/update.md §3: these names are
# the only ones a guest fetches, and the release schema refuses an unknown
# artifact name. `webapp.tar.gz` is optional, so it is absent until a release
# moves the Webapp bundle.
RELEASE_FILES = (
    ("/release.json", "release.json", "application/json"),
    ("/release.json.sig", "release.json.sig", "application/octet-stream"),
    ("/manifest.json", "manifest.json", "application/json"),
    ("/manifest.json.sig", "manifest.json.sig", "application/octet-stream"),
    ("/odile_install.py", "odile_install.py", "text/plain; charset=utf-8"),
    ("/odile_install.py.sig", "odile_install.py.sig", "application/octet-stream"),
    ("/skills.tar.gz", "skills.tar.gz", "application/gzip"),
    ("/webapp.tar.gz", "webapp.tar.gz", "application/gzip"),
)
CORE_RELEASE_FILES = (
    "release.json", "release.json.sig", "manifest.json", "manifest.json.sig",
    "odile_install.py", "odile_install.py.sig", "skills.tar.gz",
)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_app(**env_extra):
    """Start the real server on a free loopback port; return (process, port).

    Every caller passes its own temp ODILE_DB. app.py otherwise opens the live
    /var/lib/odile/waitlist.db, which holds real signups (AGENTS.md), and the
    explicit keyword here also beats any ODILE_DB inherited from the shell.
    """
    if "ODILE_DB" not in env_extra:
        raise AssertionError(
            "start_app needs an explicit ODILE_DB; app.py would open the live waitlist.db")
    port = free_port()
    env = dict(
        os.environ,
        ODILE_BIND="127.0.0.1",
        ODILE_PORT=str(port),
        ODILE_RATE_LIMIT="1000",
        **env_extra,
    )
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % port, timeout=1).status == 200:
                return proc, port
        except OSError:
            time.sleep(0.05)
    out = proc.communicate(timeout=5)[0].decode("utf-8", "replace")
    raise RuntimeError("server did not start:\n" + out)


def stop_app(proc) -> None:
    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def http_request(port, path, method="GET", fields=None, raw=False, host=None):
    """One request to a test server; the body is bytes when raw=True.

    ``host`` sets the Host header explicitly: /verify is host-gated, so a
    test that wants the page rather than the redirect must name the demo host.
    """
    body = urllib.parse.urlencode(fields) if fields is not None else None
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
    if host is not None:
        headers["Host"] = host
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res, data if raw else data.decode("utf-8")


def write_attestation(path: str, **overrides) -> None:
    """A cached report in the shape attest.py writes, for a test server."""
    data = {
        "available": True,
        "guest": "sev-snp",
        "measurement": MEASUREMENT,
        "report_version": 5,
        "report_size": 1184,
        "fetched_at": FETCHED_AT,
        "report_b64": "",
        "note": "test fixture",
    }
    data.update(overrides)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "waitlist.db")
        cls.attest = os.path.join(cls.tmp.name, "attestation.json")
        write_attestation(cls.attest)
        # Explicit role: /verify is host-gated only in the demo role, and an
        # ODILE_ROLE inherited from the shell must not choose what this tests.
        cls.proc, cls.port = start_app(
            ODILE_DB=cls.db, ODILE_ATTEST=cls.attest, ODILE_ROLE="demo")

    @classmethod
    def tearDownClass(cls):
        stop_app(cls.proc)
        cls.tmp.cleanup()

    @classmethod
    def url(cls, path: str) -> str:
        return "http://127.0.0.1:%d%s" % (cls.port, path)

    def request(self, path, method="GET", fields=None, raw=False, host=None):
        return http_request(self.port, path, method=method, fields=fields, raw=raw,
                            host=host)

    def verify(self, method="GET"):
        """The demo host's /verify: the shape the demo tenant serves."""
        return self.request("/verify", method, host="demo.odile.fyi")

    def signup(self, email="someone@example.com"):
        res, _ = self.request("/waitlist", "POST", {"email": email})
        self.assertEqual(res.status, 303, "a valid signup should redirect")
        location = res.getheader("Location") or ""
        self.assertTrue(location.startswith("/?t="), location)
        return location

    # -- the form ---------------------------------------------------------

    def test_index_offers_the_form(self):
        res, body = self.request("/")
        self.assertEqual(res.status, 200)
        self.assertIn('<form id="join"', body)
        self.assertIn("Join Waitlist", body)
        self.assertNotIn("You're on the list.", body)
        self.assertIn('<meta name="robots" content="index">', body)

    def test_index_has_no_unrendered_placeholders(self):
        _, body = self.request("/")
        self.assertNotIn("{{", body)
        self.assertNotIn("<!--done-->", body)

    # -- success ----------------------------------------------------------

    def test_signup_replaces_the_form_with_a_confirmation(self):
        email = "confirm@example.com"
        location = self.signup(email)
        res, body = self.request(location)
        self.assertEqual(res.status, 200)
        self.assertIn("You're on the list.", body)
        self.assertIn(email, body)
        self.assertIn("Stay tuned for your invite.", body)
        self.assertNotIn("/unsubscribe", body)
        self.assertNotIn("Remove this address", body)
        self.assertIn('<meta name="robots" content="noindex">', body)
        self.assertIn("<title>You&#x27;re on the list", body)

    def test_success_hides_the_email_field_and_join_button(self):
        _, body = self.request(self.signup("gone@example.com"))
        for leftover in ('<form', 'id="email"', 'name="email"', 'type="email"',
                         "Join Waitlist", 'placeholder="you@example.com"'):
            self.assertNotIn(leftover, body, "confirmation still contains %r" % leftover)

    def test_duplicate_signup_returns_the_same_confirmation(self):
        email = "twice@example.com"
        first = self.signup(email)
        second = self.signup(email)
        self.assertEqual(first, second)
        res, body = self.request(second)
        self.assertEqual(res.status, 200)
        self.assertIn("You're on the list.", body)

    # -- the event log ----------------------------------------------------

    def events_for(self, email):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT e.kind, e.detail FROM events AS e"
                " JOIN waitlist AS w ON w.id = e.waitlist_id"
                " WHERE w.email = ? ORDER BY e.id", (email,)
            ).fetchall()
        finally:
            con.close()

    def test_a_new_signup_writes_exactly_one_event(self):
        email = "event@example.com"
        self.signup(email)
        self.signup(email)          # a duplicate is not a second signup
        events = self.events_for(email)
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0][0], "signup")
        self.assertEqual(json.loads(events[0][1]), {"source": "web"})

    def test_a_refused_signup_writes_no_event(self):
        self.request("/waitlist", "POST", {"email": "not-an-email"})
        self.request("/waitlist", "POST", {"email": "bot@example.com", "hp": "spam"})
        self.assertEqual(self.events_for("not-an-email"), [])
        self.assertEqual(self.events_for("bot@example.com"), [])

    def test_removing_an_address_keeps_the_count_not_the_address(self):
        email = "removed@example.com"
        self.signup(email)
        result = run_manage(self.db, "rm", email)
        self.assertEqual(result.returncode, 0, result.stderr)
        con = sqlite3.connect(self.db)
        try:
            self.assertIsNone(
                con.execute("SELECT 1 FROM waitlist WHERE email = ?", (email,)).fetchone())
            columns = [r[1] for r in con.execute("PRAGMA table_info(events)")]
            signups = con.execute(
                "SELECT count(*) FROM events WHERE kind = 'signup'").fetchone()[0]
        finally:
            con.close()
        self.assertNotIn("email", columns, "the log must not keep the address")
        self.assertGreaterEqual(signups, 1, "the signup still counts")

    # -- failure ----------------------------------------------------------

    def test_invalid_email_keeps_the_value_and_marks_the_field(self):
        res, body = self.request("/waitlist", "POST", {"email": "not-an-email"})
        self.assertEqual(res.status, 422)
        self.assertIn("That doesn&#x27;t look like an email address.", body)
        self.assertIn('value="not-an-email"', body)
        self.assertIn('aria-invalid="true"', body)
        self.assertIn('aria-describedby="email-err"', body)
        self.assertIn('id="email-err"', body)
        self.assertIn(" autofocus", body)
        self.assertIn("Join Waitlist", body)

    def test_email_with_a_template_slot_is_echoed_literally(self):
        res, body = self.request("/waitlist", "POST", {"email": "a{{INVALID}}@x"})
        self.assertEqual(res.status, 422)
        self.assertIn('value="a{{INVALID}}@x"', body)
        tag = re.search(r'<input id="email"[^>]*>', body)
        assert tag is not None
        self.assertEqual(tag.group(0).count('aria-invalid="true"'), 1)

    def test_honeypot_is_silent_and_stores_nothing(self):
        res, _ = self.request("/waitlist", "POST", {"email": "bot@example.com", "hp": "spam"})
        self.assertEqual(res.status, 303)
        self.assertEqual(res.getheader("Location"), "/")
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT 1 FROM waitlist WHERE email = 'bot@example.com'").fetchone()
        con.close()
        self.assertIsNone(row)

    # -- no self-service removal ------------------------------------------

    def test_removed_query_falls_back_to_the_plain_form(self):
        res, body = self.request("/?removed=1")
        self.assertEqual(res.status, 200)
        self.assertIn('<form id="join"', body)
        self.assertNotIn("Removed. Nothing kept.", body)

    def test_unsubscribe_endpoint_is_gone(self):
        res, body = self.request("/unsubscribe?t=deadbeef")
        self.assertEqual(res.status, 404)
        self.assertIn("not found", body)

    def test_unknown_token_falls_back_to_the_form(self):
        res, body = self.request("/?t=deadbeef")
        self.assertEqual(res.status, 200)
        self.assertIn('<form id="join"', body)
        self.assertNotIn("You're on the list.", body)

    # -- transport --------------------------------------------------------

    def test_head_has_headers_and_no_body(self):
        res, body = self.request("/", "HEAD")
        self.assertEqual(res.status, 200)
        self.assertEqual(body, "")
        self.assertIn("default-src 'none'", res.getheader("Content-Security-Policy"))
        self.assertEqual(res.getheader("Cache-Control") or "", "no-store")

    def test_oversized_body_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/waitlist", body=b"x" * 9000,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        res = conn.getresponse()
        text = res.read().decode("utf-8")
        conn.close()
        self.assertEqual(res.status, 413)
        self.assertIn("too large", text)

    # -- the proof page ----------------------------------------------------

    def test_verify_page_shows_the_report_and_the_three_gaps(self):
        res, body = self.verify()
        self.assertEqual(res.status, 200)
        self.assertIn("text/html", res.getheader("Content-Type") or "")
        self.assertIn(MEASUREMENT, body, "the full measurement must be on the page")
        self.assertIn(FETCHED_AT, body)
        self.assertIn("version 5, 1184 bytes", body)
        self.assertIn('href="/attestation"', body)
        self.assertIn("./odile-verify https://odile.fyi", body)
        # the AMD chain status, tagged by where it is checked
        self.assertIn("AMD VCEK", body)
        self.assertIn("PASS or FAIL", body)
        self.assertIn("Where it is checked", body)
        self.assertIn("checked from your laptop", body)
        self.assertIn("read from the machine", body)
        self.assertEqual(
            str(body).count("<td>checked from your laptop</td>"),
            3,
            "every check row must keep the ODL-236 tag wording",
        )
        for gap in GAPS:
            self.assertIn(gap, body)
        positions = [body.index(gap) for gap in GAPS]
        self.assertEqual(positions, sorted(positions), "the gaps are out of order")

    def test_verify_on_odile_fyi_redirects_to_the_demo_tenants_own_page(self):
        # ODL-562: HQ's /verify put the demo's tenant sentence over HQ's own
        # guest. The HQ app now answers a 302 to the page where the sentence,
        # the report and the access report are the same machine.
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                res, body = self.request("/verify", method, host="odile.fyi")
                self.assertEqual(res.status, 302)
                self.assertEqual(res.getheader("Location"),
                                 "https://demo.odile.fyi/verify")
                self.assertEqual(body, "")
                self.assertEqual(res.getheader("Cache-Control"), "no-store")

    def test_verify_redirect_does_not_depend_on_host_case_or_port(self):
        # The Host header is split on the port and lowercased: a proxy or a
        # curl -H 'Host: ODILE.FYI:443' must get the same answer.
        for host in ("ODILE.FYI", "odile.fyi:443", "127.0.0.1"):
            with self.subTest(host=host):
                res, body = self.request("/verify", host=host)
                self.assertEqual(res.status, 302)
                self.assertEqual(res.getheader("Location"),
                                 "https://demo.odile.fyi/verify")
                self.assertEqual(body, "")

    def test_verify_on_the_demo_host_renders_the_page(self):
        # The DNS fallback (demo.odile.fyi -> HQ with the demo role) must not
        # redirect to itself; case and an explicit port are part of the Host.
        for host in ("demo.odile.fyi", "DEMO.ODILE.FYI:443"):
            with self.subTest(host=host):
                res, body = self.request("/verify", host=host)
                self.assertEqual(res.status, 200)
                self.assertIn("This is Odile's tenant machine", body)
                self.assertIn(MEASUREMENT, body)
                self.assertIn(FETCHED_AT, body)

    def test_verify_page_has_no_javascript_and_no_external_request(self):
        _, body = self.verify()
        self.assertNotIn("<script", body.lower())
        self.assertIsNone(
            re.search(r"<(?:script|link|img)\b[^>]*\b(?:src|href)=[\"']https?://", body, re.I),
            "the page makes an external request")
        self.assertNotIn("url(http", body.replace(" ", "").lower())
        # An <a> is a click, not a request. The page names no host but ours and
        # the demo's own front door: Savio made https://demo.odile.fyi/login
        # the judge's entry surface on 2026-11-14 and /judge retires to a 301.
        allowed = re.compile(r"^https://(?:odile\.fyi|demo\.odile\.fyi)(?:/[^\s\"'<>]*)?$")
        for url in re.findall(r"https?://[^\s\"'<>]+", body):
            self.assertRegex(url, allowed, "the page names an external URL: %s" % url)

    def test_verify_page_names_no_network_record(self):
        # The product tells one story — the pin in the repository. The NEAR
        # testnet anchor left no section, link or vocabulary behind (ODL-421).
        _, body = self.verify()
        self.assertNotIn("testnet", body.lower())
        self.assertNotIn("anchor", body.lower())

    def test_verify_page_has_no_unrendered_placeholders(self):
        _, body = self.verify()
        self.assertNotIn("{{", body)
        self.assertNotIn("}}", body)

    def test_verify_page_prompt_is_decorative(self):
        # Selecting the <pre> must not copy the prompt: a pasted "$" is
        # "command not found", and this command is the page's one action.
        _, body = self.verify()
        self.assertIn('class="prompt" aria-hidden="true"', body)
        self.assertRegex(body, r"\.prompt\{[^}]*user-select:none")

    def test_verify_page_machine_notation_uses_the_machine_face(self):
        # The embedded Cormorant subset carries no U+2192 or U+2026, so a bare
        # arrow or ellipsis falls back to a third serif family. Both carry the
        # mono stack instead.
        _, body = self.verify()
        self.assertIn('<span class="mono">&rarr;</span>', body)
        self.assertIn('<span class="mono">&hellip;</span>', body)
        bare = re.sub(r'<span class="mono">(?:&rarr;|&hellip;)</span>', "", body)
        self.assertNotIn("&rarr;", bare, "an arrow would render in a fallback serif")
        self.assertNotIn("&hellip;", bare, "an ellipsis would render in a fallback serif")

    def test_verify_page_keeps_machine_tokens_whole(self):
        # At 390 a break after the hyphen reads as the start of a new row.
        _, body = self.verify()
        self.assertIn('<span class="nowrap">SHA-256</span>', body)
        self.assertIn('<span class="nowrap"><code>odile-verify.pin</code></span>', body)

    def test_verify_page_adds_no_unapproved_claim(self):
        # The refresh cadence is an implementation detail: docs/CLAIMS.md has
        # no row for it, so the page may not mention it.
        _, body = self.verify()
        self.assertNotIn("cached", body.lower())
        self.assertNotIn("timer", body.lower())

    def test_verify_without_a_report_still_serves_the_page(self):
        os.unlink(self.attest)
        try:
            res, body = self.verify()
        finally:
            write_attestation(self.attest)
        self.assertEqual(res.status, 200)
        self.assertIn("unavailable", body)
        for gap in GAPS:
            self.assertIn(gap, body)

    def test_verify_escapes_a_hostile_measurement(self):
        write_attestation(self.attest, measurement="<script>alert(1)</script>")
        try:
            res, body = self.verify()
        finally:
            write_attestation(self.attest)
        self.assertEqual(res.status, 200)
        self.assertNotIn("<script", body.lower())
        self.assertIn("unavailable", body)

    def test_attestation_is_json_and_matches_the_verify_page(self):
        res, body = self.request("/attestation")
        self.assertEqual(res.status, 200)
        self.assertIn("application/json", res.getheader("Content-Type") or "")
        data = json.loads(body)
        self.assertEqual(data["measurement"], MEASUREMENT)
        _, page_body = self.verify()
        self.assertIn(data["measurement"], page_body)

    # -- the FAQ -----------------------------------------------------------

    def test_faq_page_states_the_three_gaps_before_the_first_question(self):
        res, body = self.request("/faq")
        self.assertEqual(res.status, 200)
        self.assertIn("text/html", res.getheader("Content-Type") or "")
        for gap in GAPS:
            self.assertIn(gap, body)
        positions = [body.index(gap) for gap in GAPS]
        self.assertEqual(positions, sorted(positions), "the gaps are out of order")
        self.assertIn("<dt>", body, "the FAQ has no questions")
        self.assertLess(max(positions), body.index("<dt>"),
                        "the gaps come after the first question")

    def test_faq_page_has_no_javascript_and_no_external_request(self):
        _, body = self.request("/faq")
        self.assertNotIn("<script", body.lower())
        self.assertIsNone(
            re.search(r"<(?:script|link|img)\b[^>]*\b(?:src|href)=[\"']https?://", body, re.I),
            "the page makes an external request")
        self.assertEqual(re.findall(r"https?://[^\s\"'<>]+", body), [],
                         "the page names an external URL")

    def test_faq_page_has_no_unrendered_placeholders(self):
        _, body = self.request("/faq")
        self.assertNotIn("{{", body)
        self.assertNotIn("}}", body)

    def test_the_list_links_to_the_faq_in_both_states(self):
        _, body = self.request("/")
        self.assertIn('href="/faq"', body, "the landing page does not offer the FAQ")
        _, done = self.request(self.signup("faq@example.com"))
        self.assertIn('href="/faq"', done, "the confirmation does not offer the FAQ")

    def test_the_legal_pages_are_served_without_javascript_or_a_cookie(self):
        for path, title in (
            ("/privacy", "Privacy \u2014 Odile"),
            ("/terms", "Terms \u2014 Odile"),
        ):
            with self.subTest(path=path):
                res, body = self.request(path)
                self.assertEqual(res.status, 200)
                self.assertIn("<title>%s</title>" % title, body)
                self.assertIn("text/html", res.getheader("Content-Type") or "")
                self.assertNotIn("<script", body.lower())
                self.assertIsNone(res.getheader("Set-Cookie"), "a legal page set a cookie")
                self.assertEqual(re.findall(r"https?://[^\s\"'<>]+", body), [],
                                 "the page names an external URL")
                self.assertNotIn("{{", body)
                self.assertNotIn("}}", body)

    def test_the_list_links_to_privacy_and_terms_in_both_states(self):
        for path in ("/privacy", "/terms"):
            with self.subTest(path=path):
                _, body = self.request("/")
                self.assertIn('href="%s"' % path, body,
                              "the landing page does not offer %s" % path)
        _, done = self.request(self.signup("legal@example.com"))
        for path in ("/privacy", "/terms"):
            self.assertIn('href="%s"' % path, done,
                          "the confirmation does not offer %s" % path)

    def test_abuse_is_a_permanent_redirect_to_the_terms(self):
        # ODL-903 retires the page: it repeated privacy and terms in a
        # police-blotter tone, and Savio asked for it gone (2026-11-14). The
        # address stays, so a 301 — not a 404, and not the signup flow's 303.
        # The query string is not carried; HEAD matters as much as GET.
        for path in ("/abuse", "/abuse?x=1"):
            with self.subTest(path=path):
                res, body = self.request(path)
                self.assertEqual(res.status, 301)
                self.assertEqual(res.getheader("Location"), "/terms")
                self.assertEqual(body, "")
        res, body = self.request("/abuse", "HEAD")
        self.assertEqual(res.status, 301)
        self.assertEqual(res.getheader("Location"), "/terms")
        self.assertEqual(body, "")


    # -- the judge page and the verifier bundle -------------------------

    def test_first_visit_preview_is_a_read_only_screen(self):
        res, body = self.request("/first-visit-preview")
        body = body.decode() if isinstance(body, bytes) else body
        self.assertEqual(res.status, 200)
        self.assertIn("text/html", res.getheader("Content-Type") or "")
        self.assertIn("<title>First visit — Odile</title>", body)
        self.assertIn("<h1>Set your sign-in</h1>", body)
        self.assertIn("This is the first visit. Choose the password that opens your Odile workspace.", body)
        self.assertIn("Read-only preview", body)
        # Read-only means exactly that: no form, no field, no script.
        self.assertNotIn("<form", body.lower())
        self.assertNotIn("<input", body.lower())
        self.assertNotIn("<script", body.lower())
        self.assertIn('type="button" disabled', body)
        self.assertIn('href="https://demo.odile.fyi/login"', body)
        self.assertNotIn("{{", body)

    def test_judge_is_a_permanent_redirect_to_the_tenant_sign_in(self):
        # /judge is printed on the title and end cards of the posted demo cut,
        # so the retirement is a 301 to the tenant's own sign-in — not a 404,
        # and not the signup flow's 303. The query string is not carried: the
        # target is one page, not a second front door to this service. HEAD
        # matters as much as GET: a link checker or `curl -I` must see it.
        for path in ("/judge", "/judge?x=1"):
            with self.subTest(path=path):
                res, body = self.request(path)
                self.assertEqual(res.status, 301)
                self.assertEqual(res.getheader("Location"),
                                 "https://demo.odile.fyi/login")
                self.assertEqual(body, "")
        res, body = self.request("/judge", "HEAD")
        self.assertEqual(res.status, 301)
        self.assertEqual(res.getheader("Location"), "https://demo.odile.fyi/login")
        self.assertEqual(body, "")

    def test_verify_page_offers_the_verifier_bundle(self):
        _, body = self.verify()
        self.assertIn("https://odile.fyi/odile-verify.tar.gz", body)
        self.assertIn("tar xzf odile-verify.tar.gz", body)
        self.assertIn("./odile-verify https://odile.fyi", body)
        self.assertEqual(len(re.findall(r"state state-[a-z]+", body)), 1)

    def test_verify_page_names_the_tenant_machine_and_links_to_the_login(self):
        # ODL-503 moved the public demo onto Odile's tenant machine, so the
        # demo block quotes the v3.8b deck row verbatim. Savio's 2026-11-14
        # decision makes the tenant's login page the judge's entry surface;
        # the header and foot links go there, not to the retiring /judge.
        _, body = self.verify()
        self.assertIn("This is Odile's tenant machine", body)
        self.assertIn("Odile holds this machine's boot key and can log in.", body)
        self.assertNotIn("It is not a tenant machine", body)
        self.assertEqual(body.count('href="https://demo.odile.fyi/login"'), 2)

    def test_verify_page_carries_no_retired_demo_note(self):
        # ODL-503 moved the public demo to the tenant; HQ's /verify is the
        # landing machine's proof page. The retired note promised a 00:00 UTC
        # reset the demo no longer has, and docs/CLAIMS.md v3.8b says the
        # opposite ("There is no nightly reset"), so the page drops it
        # (t_9c2d31f1).
        _, body = self.verify()
        self.assertNotIn("resets daily", body)
        self.assertNotIn("00:00 UTC", body)
        self.assertNotIn("This demo comes back without you", body)

    def test_the_bundle_extracts_and_the_verifier_runs(self):
        import io
        import tarfile
        import tempfile
        raw = urllib.request.urlopen(self.url("/odile-verify.tar.gz"), timeout=5).read()
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                names = set(tar.getnames())
                self.assertIn("odile-verify/odile-verify", names)
                self.assertIn("odile-verify/tools/snp_report.py", names)
                self.assertIn("odile-verify/odile-verify.pin", names)
                self.assertIn("odile-verify/tests/fixtures/amd/vcek.pem", names)
                tar.extractall(tmp)
            report = os.path.join(ROOT, "tests", "fixtures", "verify", "report-2026-11-06.json")
            with open(report, encoding="utf-8") as handle:
                expected = json.load(handle)["measurement"]
            result = subprocess.run(
                [sys.executable, os.path.join(tmp, "odile-verify", "odile-verify"),
                 "--report", report, "--expect", expected],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS", result.stdout)

    # -- the published signing key -----------------------------------------

    def test_signing_key_is_served_byte_for_byte(self):
        # CLAIMS.md §4.6 sends a stranger to this URL. The body must be the
        # committed key itself, not a second copy that can drift.
        with open(os.path.join(ROOT, "installer", "odile-signing-key.pub"), "rb") as handle:
            expected = handle.read()
        with urllib.request.urlopen(self.url("/odile-signing-key.pub"), timeout=5) as res:
            served = res.read()
            self.assertEqual(res.status, 200)
            self.assertEqual(res.headers.get("Content-Type"), "application/x-pem-file")
            self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(res.headers.get("Cache-Control"), "public, max-age=300")
        self.assertEqual(served, expected,
                         "the served key differs from installer/odile-signing-key.pub")
        self.assertTrue(served.startswith(b"-----BEGIN PUBLIC KEY-----\n"))

    def test_signing_key_head_has_the_headers_and_no_body(self):
        res, body = self.request("/odile-signing-key.pub", "HEAD")
        self.assertEqual(res.status, 200)
        self.assertEqual(body, "")
        self.assertEqual(res.getheader("Content-Type"), "application/x-pem-file")
        self.assertEqual(res.getheader("Cache-Control"), "public, max-age=300")

    def test_no_other_path_serves_the_key_file(self):
        # The route is one exact path; the installer directory stays unlisted.
        for path in ("/odile-signing-key.pub/",
                     "/installer/odile-signing-key.pub",
                     "/../installer/odile-signing-key.pub"):
            with self.subTest(path=path):
                res, _ = self.request(path)
                self.assertEqual(res.status, 404)

    # -- the signed release ------------------------------------------------

    def test_release_is_served_from_the_deployed_tree_byte_for_byte(self):
        # docs/runbooks/update.md §3: the guest fetches these exact names and
        # hashes the bytes against the signed release document. A transformed
        # byte is a refused update, so the body must be the file itself.
        release = os.path.join(ROOT, "release")
        seen = []
        for path, name, ctype in RELEASE_FILES:
            source = os.path.join(release, name)
            if not os.path.exists(source):
                continue
            seen.append(name)
            with open(source, "rb") as handle:
                expected = handle.read()
            res, served = self.request(path, raw=True)
            self.assertEqual(res.status, 200, "%s is not served" % path)
            self.assertEqual(served, expected,
                             "%s differs from the committed release" % path)
            self.assertEqual(res.getheader("Content-Length"), str(len(expected)))
            self.assertEqual(res.getheader("Content-Type"), ctype)
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
            self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(sorted(seen), sorted(CORE_RELEASE_FILES),
                         "the committed release is incomplete")

    def test_the_release_document_names_the_bytes_that_are_served(self):
        res, raw = self.request("/release.json", raw=True)
        self.assertEqual(res.status, 200)
        document = json.loads(raw)
        self.assertEqual(document["product"], "odile")
        self.assertEqual(document["manifest"]["url"], "manifest.json")
        _, manifest = self.request("/manifest.json", raw=True)
        self.assertEqual(hashlib.sha256(manifest).hexdigest(),
                         document["manifest"]["sha256"],
                         "the served manifest is not the one the release names")
        for name, spec in sorted(document["artifacts"].items()):
            with self.subTest(artifact=name):
                res, body = self.request("/" + spec["url"], raw=True)
                self.assertEqual(res.status, 200, name)
                self.assertEqual(len(body), spec["size"], name)
                self.assertEqual(hashlib.sha256(body).hexdigest(), spec["sha256"], name)

    def test_the_served_release_verifies_against_the_published_key(self):
        # The route serves the signed release, not a placeholder: both detached
        # signatures verify with the key the recovery kit carries.
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("release.json", "manifest.json"):
                for suffix in ("", ".sig"):
                    _, body = self.request("/" + name + suffix, raw=True)
                    with open(os.path.join(tmp, name + suffix), "wb") as handle:
                        handle.write(body)
                result = subprocess.run(
                    [sys.executable, os.path.join(ROOT, "scripts", "manifest.py"),
                     "verify",
                     "--manifest", os.path.join(tmp, name),
                     "--signature", os.path.join(tmp, name + ".sig"),
                     "--pubkey", os.path.join(ROOT, "installer", "odile-signing-key.pub")],
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("signature: OK", result.stdout)

    def test_the_served_installer_verifies_from_the_bundle_alone(self):
        # ODL-332 follow-up: a stranger who downloads the installer, its
        # detached signature and the published key from the release URLs must
        # pass --verify-signature without the repository. The signature route
        # is the missing piece; the key route is CLAIMS.md §4.6's.
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("odile_install.py", "odile_install.py.sig",
                         "odile-signing-key.pub"):
                res, body = self.request("/" + name, raw=True)
                self.assertEqual(res.status, 200, "%s is not served" % name)
                with open(os.path.join(tmp, name), "wb") as handle:
                    handle.write(body)
            result = subprocess.run(
                [sys.executable, os.path.join(tmp, "odile_install.py"),
                 "--verify-signature",
                 "--pubkey", os.path.join(tmp, "odile-signing-key.pub")],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("signature: OK", result.stdout)

    def test_the_release_key_copy_is_the_published_key(self):
        # release/ is mirrorable as one directory; its key must be the one the
        # installer and the /odile-signing-key.pub route read.
        with open(os.path.join(ROOT, "installer", "odile-signing-key.pub"), "rb") as handle:
            published = handle.read()
        with open(os.path.join(ROOT, "release", "odile-signing-key.pub"), "rb") as handle:
            carried = handle.read()
        self.assertEqual(carried, published,
                         "release/odile-signing-key.pub differs from installer/odile-signing-key.pub")

    def test_the_release_names_the_committed_installer_pair(self):
        # The download a stranger gets must be the pair this tree signs. A
        # release cut before an installer re-embed names older bytes, so
        # --verify-signature fails on the release copy while the checkout is
        # green (measured on 2026.11.09-1, re-cut by ODL-234's follow-up).
        with open(os.path.join(ROOT, "release", "release.json"), "rb") as handle:
            document = json.load(handle)
        with open(os.path.join(ROOT, "release", "manifest.json"), "rb") as handle:
            manifest = json.load(handle)
        with open(os.path.join(ROOT, "installer", "odile_install.py"), "rb") as handle:
            installer = handle.read()
        digest = hashlib.sha256(installer).hexdigest()
        self.assertEqual(document["artifacts"]["installer"]["sha256"], digest,
                         "release.json does not name the committed installer")
        self.assertEqual(document["artifacts"]["installer"]["size"], len(installer))
        self.assertEqual(manifest["installer_sha256"], digest,
                         "the release manifest does not name the committed installer")
        for name in ("odile_install.py", "odile_install.py.sig"):
            with open(os.path.join(ROOT, "installer", name), "rb") as handle:
                committed = handle.read()
            with open(os.path.join(ROOT, "release", name), "rb") as handle:
                carried = handle.read()
            self.assertEqual(carried, committed,
                             "release/%s differs from installer/%s" % (name, name))

    def test_the_release_skills_bundle_carries_every_tenant_skill(self):
        # The skills tarball is the app layer a guest applies, keyed relative
        # to $HERMES_HOME/skills. A bundle cut before a skill was embedded
        # leaves that skill off the machine even though every pin inside the
        # installer still matches (ODL-234's follow-up).
        with open(os.path.join(ROOT, "release", "skills.tar.gz"), "rb") as handle:
            bundle = handle.read()
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
            names = set(archive.getnames())
        for skill in ("odile-first-run", "odile-help", "odile-recipes"):
            self.assertIn(skill + "/SKILL.md", names,
                          "%s/SKILL.md is not in the release bundle" % skill)

    def test_no_other_path_serves_the_release_directory(self):
        for path in ("/release", "/release/", "/release/skills.tar.gz.bak",
                     "/release/../app.py", "/app.py", "/odile_install.py.bak",
                     "/release.json/"):
            with self.subTest(path=path):
                res, _ = self.request(path)
                self.assertEqual(res.status, 404)

    def test_release_head_has_the_headers_and_no_body(self):
        res, body = self.request("/manifest.json", "HEAD")
        self.assertEqual(res.status, 200)
        self.assertEqual(body, "")
        self.assertEqual(res.getheader("Content-Type"), "application/json")
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        self.assertEqual(
            res.getheader("Content-Length"),
            str(os.path.getsize(os.path.join(ROOT, "release", "manifest.json"))))


class TenantVerify(unittest.TestCase):
    """ODILE_ROLE=tenant: /verify is the machine's own page, on any Host.

    The redirect is a demo-role (HQ) answer only. A tenant machine has no
    demo page to send a reader to, and its page is already the machine's own:
    the report, the access report and the public sentence are one machine.
    """

    AT = "2026-11-11T10:00:00Z"
    ROWS = (
        "  usable keys    PASS  0 usable user keys; every authorized_keys line is the measured boot key",
        "  boot key       PASS  ssh-ed25519 SHA256:LLwDfYyE3VMc9T9o+XvydfNzrXxyzaQ2lysSxCSSEWA; public key only",
        "  seal           PASS  policy 0x30000; seal_id strict-bench-v1; measurement " + "a" * 96,
        "  sign-in hash   PASS  scrypt hash present in /home/odile/.hermes/.env; no plaintext password",
        "  operator key   PASS  none (authorized_keys: 1 line(s), the measured throwaway)",
    )
    SUMMARY = ("PASS \u2014 five checks: zero usable user keys, the throwaway boot key, "
               "the seal, the sign-in hash, no operator key.")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "waitlist.db")
        cls.attest = os.path.join(cls.tmp.name, "attestation.json")
        cls.report = os.path.join(cls.tmp.name, "access-report.txt")
        write_attestation(cls.attest)
        with open(cls.report, "w", encoding="utf-8") as handle:
            handle.write("read from your machine, %s\n" % cls.AT)
            handle.write("verify_and_unlock \u2014 access report\n\n")
            handle.write("\n".join(cls.ROWS) + "\n\n")
            handle.write("  %s\n" % cls.SUMMARY)
        cls.proc, cls.port = start_app(
            ODILE_DB=cls.db, ODILE_ATTEST=cls.attest,
            ODILE_ROLE="tenant", ODILE_ACCESS_REPORT=cls.report)

    @classmethod
    def tearDownClass(cls):
        stop_app(cls.proc)
        cls.tmp.cleanup()

    def request(self, path, method="GET", host=None):
        return http_request(self.port, path, method=method, host=host)

    def test_a_tenant_renders_its_own_page_on_every_host(self):
        # No redirect: a tenant's /verify is not the demo's page, so the
        # odile.fyi Host must answer with this machine's own report.
        for host in ("odile.fyi", "demo.odile.fyi"):
            with self.subTest(host=host):
                res, body = self.request("/verify", host=host)
                self.assertEqual(res.status, 200)
                self.assertIn(MEASUREMENT, body)
                self.assertIn(FETCHED_AT, body)
                self.assertIn("read from your machine", body)
                self.assertIn("usable keys    PASS", body)
                self.assertIn("No one at Odile can log in to a tenant machine.", body)
                self.assertNotIn("This is Odile's tenant machine", body)
                self.assertNotIn("from a test machine", body)

    def test_a_tenant_page_keeps_the_three_gaps(self):
        _, body = self.request("/verify", host="odile.fyi")
        for gap in GAPS:
            self.assertIn(gap, body)
        positions = [body.index(gap) for gap in GAPS]
        self.assertEqual(positions, sorted(positions), "the gaps are out of order")

    def test_a_tenant_head_verify_has_no_body(self):
        res, body = self.request("/verify", "HEAD", host="odile.fyi")
        self.assertEqual(res.status, 200)
        self.assertEqual(body, "")


class ReleaseServing(unittest.TestCase):
    """The release route against a directory the test owns, so it can change."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.release = os.path.join(cls.tmp.name, "release")
        os.makedirs(cls.release)
        # The app opens and migrates a database at startup; keep it away from
        # /var/lib/odile/waitlist.db, which holds real signups.
        cls.proc, cls.port = start_app(
            ODILE_RELEASE_DIR=cls.release,
            ODILE_DB=os.path.join(cls.tmp.name, "waitlist.db"),
            ODILE_ATTEST=os.path.join(cls.tmp.name, "attestation.json"),
        )

    @classmethod
    def tearDownClass(cls):
        stop_app(cls.proc)
        cls.tmp.cleanup()

    def setUp(self):
        for name in os.listdir(self.release):
            os.unlink(os.path.join(self.release, name))

    def write(self, name: str, data: bytes) -> None:
        with open(os.path.join(self.release, name), "wb") as handle:
            handle.write(data)

    def get(self, path):
        return http_request(self.port, path, raw=True)

    def test_a_replaced_release_is_served_without_a_restart(self):
        # A release is never served stale: each request reads the directory,
        # so publishing a new release needs no service restart.
        first_doc = b'{"release": "2026.11.08-1"}\n'
        second_doc = b'{"release": "2026.11.08-2", "notes": "a longer document"}\n'
        self.write("release.json", first_doc)
        res, first = self.get("/release.json")
        self.assertEqual((res.status, first), (200, first_doc))
        self.write("release.json", second_doc)
        res, second = self.get("/release.json")
        self.assertEqual(res.status, 200)
        self.assertEqual(second, second_doc)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        self.assertEqual(res.getheader("Content-Length"), str(len(second_doc)))

    def test_a_missing_file_is_a_404_not_a_500(self):
        self.write("release.json", b"{}\n")
        for path in ("/release.json.sig", "/manifest.json", "/skills.tar.gz"):
            with self.subTest(path=path):
                res, body = self.get(path)
                self.assertEqual(res.status, 404)
                self.assertEqual(body, b"not found\n")
                self.assertEqual(res.getheader("Cache-Control"), "no-store")

    def test_a_file_with_another_name_is_not_served(self):
        # The release names fixed artifacts; a stray file in the directory is
        # not a URL. `webapp.tar.gz` is the optional fourth artifact.
        self.write("app.py", b"print('not a release artifact')\n")
        self.write("webapp.tar.gz", b"a webapp bundle")
        for path in ("/app.py", "/release/app.py", "/other.tar.gz"):
            with self.subTest(path=path):
                res, _ = self.get(path)
                self.assertEqual(res.status, 404)
        res, body = self.get("/webapp.tar.gz")
        self.assertEqual((res.status, body), (200, b"a webapp bundle"))
        self.assertEqual(res.getheader("Content-Type"), "application/gzip")

    def test_a_missing_release_directory_is_a_404_not_a_500(self):
        os.rmdir(self.release)
        try:
            res, body = self.get("/release.json")
        finally:
            os.makedirs(self.release)
        self.assertEqual((res.status, body), (404, b"not found\n"))

# ---------------------------------------------------------------- schema v2

LEGACY_SCHEMA = """
CREATE TABLE waitlist (
    id           INTEGER PRIMARY KEY,
    email        TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL,
    token        TEXT    NOT NULL UNIQUE,
    source       TEXT    NOT NULL DEFAULT 'web',
    unsubscribed INTEGER NOT NULL DEFAULT 0
);
"""


def iso(days_ago: float = 0) -> str:
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_legacy_db(path: str, rows=()) -> None:
    """A pre-v2 database: one table, holding the signups made before the spine."""
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    for n, (email, created_at) in enumerate(rows, 1):
        con.execute(
            "INSERT INTO waitlist (email, created_at, token) VALUES (?, ?, ?)",
            (email, created_at, "token-%02d" % n),
        )
    con.commit()
    con.close()


def run_manage(db_path: str, *args):
    env = dict(os.environ, ODILE_DB=db_path)
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "manage.py"), *args],
        env=env, capture_output=True, text=True, timeout=30,
    )


class Migration(unittest.TestCase):
    """The v3 schema on a copy of a v2 database, a v1 database, and an empty file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "waitlist.db")

    def tearDown(self):
        self.tmp.cleanup()

    def migrate(self):
        result = run_manage(self.db, "migrate")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def connect(self):
        return sqlite3.connect(self.db)

    def tables(self):
        con = self.connect()
        try:
            return {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            con.close()

    def test_migrate_builds_the_spine_and_keeps_the_signups(self):
        make_legacy_db(self.db, [
            ("first@example.com", "2026-11-04T06:12:06Z"),
            ("second@example.com", "2026-11-04T08:34:06Z"),
        ])
        result = self.migrate()
        self.assertTrue(
            {"waitlist", "tenants", "claims", "events", "grants"} <= self.tables(),
            self.tables())
        con = self.connect()
        try:
            signups = con.execute(
                "SELECT email, created_at, source, unsubscribed FROM waitlist ORDER BY id"
            ).fetchall()
            events = con.execute(
                "SELECT waitlist_id, kind, created_at, detail FROM events ORDER BY id"
            ).fetchall()
            version = con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(signups, [
            ("first@example.com", "2026-11-04T06:12:06Z", "web", 0),
            ("second@example.com", "2026-11-04T08:34:06Z", "web", 0),
        ])
        self.assertEqual(
            [(row[0], row[1], row[2], json.loads(row[3])) for row in events],
            [
                (1, "signup", "2026-11-04T06:12:06Z", {"source": "web"}),
                (2, "signup", "2026-11-04T08:34:06Z", {"source": "web"}),
            ])
        self.assertEqual(version, 3)
        self.assertIn("backfilled 2 signup event(s)", result.stdout)

    def test_migrate_twice_changes_nothing(self):
        make_legacy_db(self.db, [("twice@example.com", "2026-11-04T06:12:06Z")])
        self.migrate()
        before = self.connect()
        try:
            waitlist = before.execute(
                "SELECT email, created_at, token FROM waitlist ORDER BY id").fetchall()
            events = before.execute("SELECT count(*) FROM events").fetchone()[0]
        finally:
            before.close()
        second = self.migrate()
        self.assertIn("already present", second.stdout)
        after = self.connect()
        try:
            self.assertEqual(
                after.execute("SELECT email, created_at, token FROM waitlist ORDER BY id").fetchall(),
                waitlist)
            self.assertEqual(after.execute("SELECT count(*) FROM events").fetchone()[0], events)
        finally:
            after.close()
        self.assertNotIn("backfilled", second.stdout)

    def test_migrate_twice_on_a_v2_copy_adds_release_and_keeps_rows(self):
        """A copy of the live schema gains `release`; a second run is a no-op.

        The live database is a v2 spine with tenants in it. Reproduce that
        shape by building the current schema and taking the new column away,
        then run the real `manage.py migrate` twice: the first run adds the
        column, the second says every table is already present, and the
        existing tenant row is byte-for-byte the same.
        """
        import app

        con = sqlite3.connect(self.db)
        try:
            con.executescript(app.SCHEMA)
            con.execute("ALTER TABLE tenants DROP COLUMN release")
            con.execute("PRAGMA user_version = 2")
            con.execute(
                "INSERT INTO tenants (slug, email, created_at, state, plan)"
                " VALUES ('odl-aa11bb', 'ada@example.com', ?, 'invited', 'plus')",
                (iso(),),
            )
            con.commit()
        finally:
            con.close()

        first = self.migrate()
        self.assertIn("migrate: ok", first.stdout)
        self.assertIn("added tenants.release", first.stdout)
        second = self.migrate()
        self.assertIn("already present", second.stdout)
        self.assertNotIn("added tenants.release", second.stdout)

        con = self.connect()
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(tenants)")}
            row = con.execute(
                "SELECT slug, email, state, plan, release FROM tenants"
            ).fetchone()
            version = con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
        self.assertIn("release", columns)
        self.assertEqual(row, ("odl-aa11bb", "ada@example.com", "invited", "plus", None))
        self.assertEqual(version, 3)

    def test_migrate_on_an_empty_file_creates_the_whole_schema(self):
        self.migrate()
        self.assertEqual(
            self.tables(), {"waitlist", "tenants", "claims", "events", "grants"})
        funnel = run_manage(self.db, "funnel")
        self.assertEqual(funnel.returncode, 0, funnel.stderr)
        self.assertIn("signups", funnel.stdout)

    def test_the_service_boot_migrates_a_legacy_database(self):
        import app
        make_legacy_db(self.db, [("boot@example.com", "2026-11-04T06:12:06Z")])
        original = app.DB_PATH
        app.DB_PATH = self.db
        try:
            app.init_db()
        finally:
            app.DB_PATH = original
        con = self.connect()
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM waitlist").fetchone()[0], 1)
            self.assertEqual(
                con.execute("SELECT count(*) FROM events WHERE kind = 'signup'").fetchone()[0], 1)
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 3)
        finally:
            con.close()

    def test_no_password_or_private_key_columns(self):
        make_legacy_db(self.db)
        self.migrate()
        banned = re.compile(r"pass(word|phrase)?|private|secret", re.I)
        con = self.connect()
        try:
            columns = {
                table: [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
                for table in ("waitlist", "tenants", "claims", "events", "grants")
            }
        finally:
            con.close()
        for table, cols in columns.items():
            for col in cols:
                self.assertIsNone(banned.search(col), "%s.%s stores a secret" % (table, col))
        self.assertIn("ssh_public_key", columns["tenants"])
        self.assertIn("setup_token_hash", columns["tenants"])
        self.assertIn("token_hash", columns["claims"])
        # A signup event points at the waitlist row, never at the address.
        self.assertIn("waitlist_id", columns["events"])
        self.assertNotIn("email", columns["events"])

    def test_events_cannot_be_updated_or_deleted(self):
        make_legacy_db(self.db)
        self.migrate()
        con = self.connect()
        try:
            con.execute(
                "INSERT INTO waitlist (email, created_at, token) VALUES (?, ?, 'token-log')",
                ("log@example.com", iso()))
            waitlist_id = con.execute("SELECT id FROM waitlist").fetchone()[0]
            con.execute(
                "INSERT INTO events (waitlist_id, kind, detail, created_at)"
                " VALUES (?, 'signup', '{}', ?)", (waitlist_id, iso()))
            con.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE events SET kind = 'churned' WHERE waitlist_id = ?",
                            (waitlist_id,))
            con.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM events WHERE waitlist_id = ?", (waitlist_id,))
            con.rollback()
            self.assertEqual(
                con.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        finally:
            con.close()

    def test_a_claim_is_single_use(self):
        make_legacy_db(self.db)
        self.migrate()
        con = self.connect()
        try:
            con.execute(
                "INSERT INTO tenants (slug, email, created_at) VALUES ('alpha', 'alpha@example.com', ?)",
                (iso(),))
            tenant = con.execute("SELECT id FROM tenants").fetchone()[0]
            con.execute(
                "INSERT INTO claims (tenant_id, token_hash, created_at, expires_at)"
                " VALUES (?, 'sha256-of-the-token', ?, ?)",
                (tenant, iso(), iso(-1)))
            con.commit()
            first = con.execute(
                "UPDATE claims SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                (iso(), "sha256-of-the-token")).rowcount
            second = con.execute(
                "UPDATE claims SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                (iso(), "sha256-of-the-token")).rowcount
            self.assertEqual((first, second), (1, 0))
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE claims SET used_at = ?", (iso(),))
            con.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO claims (tenant_id, token_hash, created_at, expires_at)"
                    " VALUES (?, 'sha256-of-the-token', ?, ?)",
                    (tenant, iso(), iso(-1)))
            con.rollback()
        finally:
            con.close()


class Funnel(unittest.TestCase):
    """`manage.py funnel` reads the spine, stage by stage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "waitlist.db")
        result = run_manage(self.db, "migrate")
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, stdout: str, label: str) -> int:
        match = re.search(r"^%s\s+(\d+)$" % re.escape(label), stdout, re.M)
        if match is None:
            self.fail("no %r stage in:\n%s" % (label, stdout))
        return int(match.group(1))

    def seed(self):
        con = sqlite3.connect(self.db)
        try:
            for email in ("one@example.com", "two@example.com", "three@example.com"):
                con.execute(
                    "INSERT INTO waitlist (email, created_at, token) VALUES (?, ?, ?)",
                    (email, iso(30), "token-" + email))
            now = iso()
            old = iso(8)
            tenants = [
                # slug, email, key, state, claimed_at, installed_at, heartbeat
                ("alpha", "alpha@example.com", "ssh-ed25519 AAAA alpha", "active", now, now, now),
                ("beta", "beta@example.com", None, "invited", None, None, None),
                ("gamma", "gamma@example.com", "ssh-ed25519 AAAA gamma",
                 "waiting_for_owner", now, now, old),
            ]
            for slug, email, key, state, claimed, installed, beat in tenants:
                con.execute(
                    "INSERT INTO tenants (slug, email, ssh_public_key, state, claimed_at,"
                    " installed_at, last_heartbeat, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (slug, email, key, state, claimed, installed, beat, iso(20)))
                tenant = con.execute("SELECT id FROM tenants WHERE slug = ?", (slug,)).fetchone()[0]
                con.execute(
                    "INSERT INTO events (tenant_id, kind, detail, created_at)"
                    " VALUES (?, 'invited', '{}', ?)", (tenant, iso(20)))
            alpha = con.execute("SELECT id FROM tenants WHERE slug = 'alpha'").fetchone()[0]
            con.execute(
                "INSERT INTO events (tenant_id, kind, detail, created_at)"
                " VALUES (?, 'activated', '{}', ?)", (alpha, now))
            con.commit()
        finally:
            con.close()

    def test_funnel_prints_every_stage_in_order_with_real_counts(self):
        self.seed()
        result = run_manage(self.db, "funnel")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertEqual(self.stage(out, "signups"), 3)
        self.assertEqual(self.stage(out, "invited"), 3)
        self.assertEqual(self.stage(out, "claimed"), 2)
        self.assertEqual(self.stage(out, "installed"), 2)
        self.assertEqual(self.stage(out, "activated"), 1)
        self.assertEqual(self.stage(out, "active in last 7 days"), 1)
        order = [out.index(label) for label in
                 ("signups", "invited", "claimed", "installed", "activated",
                  "active in last 7 days")]
        self.assertEqual(order, sorted(order))

    def test_funnel_on_an_empty_list_prints_zeros(self):
        result = run_manage(self.db, "funnel")
        self.assertEqual(result.returncode, 0, result.stderr)
        for label in ("signups", "invited", "claimed", "installed", "activated",
                      "active in last 7 days"):
            self.assertEqual(self.stage(result.stdout, label), 0)


class AttestRefresh(unittest.TestCase):
    """The refresh wrapper: one complete JSON document, never a partial write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.out = os.path.join(self.dir, "attestation.json")
        self.wrapper = WRAPPER

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, script: str) -> dict:
        return dict(os.environ, ODILE_ATTEST_PY=script, ODILE_ATTEST_OUT=self.out)

    def run_wrapper(self, script: str, **kwargs):
        return subprocess.run(
            ["/bin/sh", self.wrapper], env=self.env(script),
            capture_output=True, text=True, timeout=30, **kwargs,
        )

    def leftovers(self) -> list[str]:
        return [name for name in os.listdir(self.dir) if name.startswith("attestation.json.")]

    def test_the_wrapper_writes_a_complete_report(self):
        result = self.run_wrapper(os.path.join(ROOT, "attest.py"))
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("available", data)
        self.assertIn("fetched_at", data)
        self.assertEqual(self.leftovers(), [], "the temp file survived the rename")

    def test_a_reader_never_sees_a_partial_report(self):
        old = {"available": False, "sentinel": "old"}
        with open(self.out, "w", encoding="utf-8") as handle:
            json.dump(old, handle)
        new = {"available": True, "measurement": MEASUREMENT, "fetched_at": FETCHED_AT}
        fake = os.path.join(self.dir, "slow_attest.py")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write(
                "import json, sys, time\n"
                "time.sleep(0.5)\n"
                "json.dump(%r, open(sys.argv[1], 'w'))\n" % (new,)
            )
        proc = subprocess.Popen(
            ["/bin/sh", self.wrapper], env=self.env(fake),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        seen = []
        saw_temp = False
        try:
            while proc.poll() is None:
                with open(self.out, encoding="utf-8") as handle:
                    seen.append(json.load(handle))
                saw_temp = saw_temp or len(self.leftovers()) == 1
                time.sleep(0.01)
        finally:
            _, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
        self.assertGreater(len(seen), 0, "the poll loop never ran")
        for value in seen:
            self.assertIn(value, (old, new), "a reader saw a partial report")
        self.assertIn(old, seen, "the old report was never readable")
        self.assertTrue(saw_temp, "the report was written in place, not through a temp file")
        with open(self.out, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), new)
        self.assertEqual(self.leftovers(), [], "the temp file survived the rename")

    def test_a_failed_fetch_leaves_the_old_report_in_place(self):
        old = {"available": False, "sentinel": "old"}
        with open(self.out, "w", encoding="utf-8") as handle:
            json.dump(old, handle)
        fake = os.path.join(self.dir, "broken_attest.py")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write("import sys\nraise SystemExit(3)\n")
        result = self.run_wrapper(fake)
        self.assertNotEqual(result.returncode, 0)
        with open(self.out, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), old)
        self.assertEqual(self.leftovers(), [], "a failed run left its temp file behind")


class StatusPage(unittest.TestCase):
    """`/status` on the real server, from a real heartbeat history.

    The fixture is the same shape the poll writes: a tenant that reached
    `ready`, beats every five minutes, and a 90 s reboot between two beats.  The
    page is computed on every request, so these assertions are the served bytes.
    """

    @classmethod
    def setUpClass(cls):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from odilectl import health as heartbeat_health
        from odilectl import state as tenant_state

        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "waitlist.db")
        cls.now = int(time.time())
        cls.slug = "odl-stat01"
        cls.hostname = cls.slug
        start = cls.now - 2 * 3600
        reboot = cls.now - 3600
        clock = [start]
        store = tenant_state.StateStore.open(cls.db, clock=lambda: clock[0])
        beats = heartbeat_health.HeartbeatStore.open(
            cls.db, clock=lambda: clock[0], verifier=lambda *_args: True
        )
        try:
            store.invite(cls.slug, detail={"owner_email": "ada@example.com"})
            store.claim(cls.slug)
            store.provision(
                cls.slug,
                server_id="srv-stat01",
                hostname=cls.hostname,
                ipv4="203.0.113.30",
                plan_id=113,
                expires_at=cls.now + 30 * 86400,
                initramfs_host_key="SHA256:" + "A" * 43,
            )
            store.await_boot(cls.slug)
            store.start_first_visit(cls.slug)
            store.ready(cls.slug)
            moment = start
            while moment <= cls.now:
                if not (reboot < moment < reboot + 90):
                    body = json.dumps(
                        {
                            "slug": cls.slug,
                            "at": moment,
                            "public_key": "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----",
                            "facts": {"uptime_seconds": 60},
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                    beats.record(body, b"\x00" * 64, now=moment)
                moment += 300
        finally:
            beats.close()
            store.close()
        cls.proc, cls.port = start_app(ODILE_DB=cls.db)

    @classmethod
    def tearDownClass(cls):
        stop_app(cls.proc)
        cls.tmp.cleanup()

    def request(self, path, method="GET"):
        return http_request(self.port, path, method=method)

    def test_status_page_is_served_with_the_two_deck_rows(self):
        res, body = self.request("/status")
        self.assertEqual(res.status, 200)
        self.assertTrue((res.getheader("Content-Type") or "").startswith("text/html"))
        for row in (
            "Runs around the clock. Uptime is published on the status page.",
            "The status page counts uptime and names no tenant.",
        ):
            self.assertIn(row, body)
        self.assertIn("100.0%", body)
        self.assertNotIn("Waiting for its owner", body)
        self.assertNotIn("owner-waiting", body)
        self.assertNotIn("{{", body)
        self.assertNotIn("<script", body.lower())
        self.assertNotIn(self.slug, body)
        self.assertNotIn("ada@example.com", body)

    def test_a_sealed_reboot_keeps_the_url_and_writes_no_incident(self):
        """The reboot is a 90 s gap: same hostname, 100% uptime, no incident."""
        con = sqlite3.connect(self.db)
        try:
            hostname = con.execute(
                "SELECT hostname FROM tenant_state WHERE slug = ?", (self.slug,)
            ).fetchone()[0]
            parked = con.execute(
                "SELECT count(*) FROM tenant_state_events WHERE to_state = 'waiting_for_owner'"
            ).fetchone()[0]
            alerts = con.execute(
                "SELECT count(*) FROM health_alerts WHERE kind = 'waiting_for_owner'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(hostname, self.hostname, "the Webapp URL is derived from the hostname")
        self.assertEqual(parked, 0, "the reboot wrote a waiting_for_owner event")
        self.assertEqual(alerts, 0, "the reboot wrote a waiting_for_owner alert")
        res, body = self.request("/status")
        self.assertEqual(res.status, 200)
        self.assertIn("100.0%", body)
        self.assertIn("No incidents in the last 30 days.", body)

    def test_terms_points_at_the_status_page_it_names(self):
        """The deck says 'Both published on the status page'; the terms link it.

        Rendered text is unchanged: only the phrase 'the status page' becomes
        an anchor, and the reliability row still reads verbatim once the tags
        are stripped.
        """
        res, body = self.request("/terms")
        self.assertEqual(res.status, 200)
        self.assertIn('href="/status"', body,
                      "the terms page names a status page it does not link")
        text = body.decode("utf-8") if isinstance(body, bytes) else body
        self.assertIn(
            "Runs around the clock. Uptime is published on the status page.",
            re.sub(r"<[^>]+>", "", text),
        )

    def test_head_status_has_the_headers_and_no_body(self):
        res, body = self.request("/status", method="HEAD")
        self.assertEqual(res.status, 200)
        self.assertEqual(body, "")
        self.assertTrue((res.getheader("Content-Type") or "").startswith("text/html"))

    def test_status_is_503_before_the_history_exists(self):
        """A fresh database is one line of 503, not a traceback."""
        with tempfile.TemporaryDirectory() as tmp:
            proc, port = start_app(ODILE_DB=os.path.join(tmp, "empty.db"))
            try:
                res, body = http_request(port, "/status")
                self.assertEqual(res.status, 503)
                self.assertIn("no history yet", body)
            finally:
                stop_app(proc)


if __name__ == "__main__":
    unittest.main()
