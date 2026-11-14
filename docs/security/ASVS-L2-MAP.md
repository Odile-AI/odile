# ASVS 5.0 Level 2 — the odile.fyi surface, mapped

**Unit:** S1 (security track, cycle 1) · **Seat:** security · **Date:** 12 November 2026
**Scope:** the waitlist service on `odile.fyi` — every route `app.py` serves,
behind Caddy 2.6.2 on the same host (`/etc/caddy/Caddyfile`, `admin off`).
Not in scope: the tenant guest (its own trust boundary, `docs/CLAIMS.md` §4),
`demo.odile.fyi` (the tenant demo; the HQ demo's threat note was retired with
the HQ demo on 2026-11-13 — git history is the record), and the HQ Hermes
dashboard behind `gw.odile.fyi` (an operator surface, not this service).
**Standard:** OWASP ASVS 5.0, Level 2, requirement IDs as published in
`OWASP/ASVS` `5.0/en`. A "pass" below means no L2 requirement was found unmet
by reading the code and measuring the live surface; it is not a certified
assessment.

## Method

1. The route list came from `app.py` (`_dispatch`, `do_POST`), not from a
   scanner, so nothing unserved is counted and nothing served is missed.
2. Status, `Content-Type`, `Cache-Control` and the four security headers were
   measured on every route: `docs/ops/evidence/2026-11-12-security/surface.txt`.
3. The input, output, SQL, token and crypto claims were read in the code, with
   the line named in the table.
4. 4.2.1 was tested, not read: one `POST` to a refused path whose body is a
   complete `GET`, then the next request on the same connection, first against
   the app and then through Caddy.

## The map

| Chapter | Applies | L2 verdict | Evidence |
|---|---|---|---|
| V1 Encoding and Sanitization | yes | pass | Email, token, error text and title go through `html.escape` (`app.py:378,388,444,464`); every query is parameterised (`?` placeholders, no f-string SQL); no `eval`, no XML, no deserialisation; CSP `default-src 'none'` (`app.py:724`) |
| V2 Validation and Business Logic | yes | pass, one note | `EMAIL_RE` + length bound (`app.py:102,391`); consent tick; claim state machine with expiry and settled states (`app.py:937–1014`); 6 signups/10 min per IP (`app.py:324`). Note: the key is the first `X-Forwarded-For` element, and Caddy 2.6.2 **overwrites** that header with the peer (measured, `caddy-xff.txt`), so 2.4.1 holds today — see open finding 3 |
| V3 Web Frontend Security | yes | pass | No cookies, no JavaScript, no third-party request. CSP with `frame-ancestors 'none'`, `base-uri 'none'`, `form-action 'self'`; `nosniff`; `Referrer-Policy: no-referrer`; HSTS 1 year + `includeSubDomains` on every response (`surface.txt`) |
| V4 API and Web Service | yes | **gap — 4.2.1** | §"The gap" below. 4.1.1 pass (a `Content-Type` on every response, `surface.txt`); 4.1.2 pass; 4.1.3 pass by measurement (`caddy-xff.txt`), fragile — open finding 3 |
| V5 File Handling | yes | pass | Release files are an exact-path map of nine names (`app.py:61`); the verifier bundle is built in memory from a fixed list (`app.py:92`); `bg-*.webp` is `os.path.basename`-ed; no upload route on this service |
| V6 Authentication | no | n/a | No login on `odile.fyi`. The claim link is an invitation, not an authentication factor: 24 random bytes, stored as a SHA-256 hash (`odilectl/claim.py:245,252`) |
| V7 Session Management | no | n/a | No session, no cookie |
| V8 Authorization | no | n/a | Every route is public by design |
| V9 Self-contained Tokens | no | n/a | The claim token is opaque and DB-backed, not a JWT |
| V10 OAuth and OIDC | no | n/a | — |
| V11 Cryptography | yes | pass | `secrets.token_hex` for both tokens; claim tokens hashed with SHA-256; the published signing key is Ed25519 PEM (`installer/odile-signing-key.pub`); no password is stored |
| V12 Secure Communication | yes | pass | TLS 1.2 and 1.3 measured (`openssl s_client`); HSTS on every route; Caddy `admin off`; plain-HTTP host redirects to the canonical name |
| V13 Configuration | yes | pass | No version banner (`Server: odile`); no directory listing; `/.git/config` 404; `TRACE` and `OPTIONS` 501; no debug or docs endpoint (the unlisted `/roadmap` is a page, not an API) |
| V14 Data Protection | yes | pass, one note | `Cache-Control: no-store` on every route except the two public artefacts (5 min); no third-party request; email travels in the request body. Note: the waitlist confirmation token (96 bits) is stored raw — see open finding 4 |
| V15 Secure Coding and Architecture | yes | pass | Standard library only; a lock around the in-memory limiter; one schema constant shared by service and migration; no dynamic code |
| V16 Security Logging and Error Handling | yes | **partly met** | 16.5.1 is the visible symptom of the 4.2.1 gap: the app's default 501 page echoes the parsed method (`Unsupported method ('abcGET')`). 16.1.1/16.3.3 are not met: there is no security-event log at all — open finding 2 |
| V17 WebRTC | no | n/a | — |

## The gap: 4.2.1, HTTP message boundaries

ASVS 5.0 **4.2.1** (L2): "Verify that all application components (including
load balancers, firewalls, and application servers) determine boundaries of
incoming HTTP messages using the appropriate mechanism for the HTTP version to
prevent HTTP request smuggling. In HTTP/1.x, if a Transfer-Encoding header
field is present, the Content-Length header must be ignored per RFC 2616 …"

RFC 9112 §9.3 states the same duty for a persistent connection: "A server MUST
read the entire request message body or close the connection after sending its
response; otherwise, the remaining data on a persistent connection would be
misinterpreted as the next request."

`do_POST` answers 405 before it reads `Content-Length` (`app.py:853–856`), and
`_send` leaves the connection open. The body therefore stays in the stream, and
`BaseHTTPRequestHandler` parses it as the next request line.

Reproduced on the live site, 2026-11-12 06:48Z, three commands, no write:

```console
$ curl -sS -o /dev/null -w '%{http_code}\n' -X POST --data 'abc' https://odile.fyi/nope
405
$ curl -sS -o /dev/null -w '%{http_code}\n' https://odile.fyi/
501
$ curl -sS -o /dev/null -w '%{http_code}\n' https://odile.fyi/
200
```

The 501 body is the app's default error page and names the attacker's bytes:
`Unsupported method ('abcGET')` — the `abc` from the POST body concatenated
with the next request's `GET`. Caddy has pooled the upstream connection, so the
next visitor's request consumed the poisoned line; the following request opened
a fresh connection and recovered.

**Blast radius.** Any client can make the next request on a reused connection
return a response of the attacker's choosing from the app's own vocabulary: a
body that is a complete `GET /attestation HTTP/1.1` makes the next visitor's
page request answer with the attestation JSON; a bare token answers 501. It is
a one-request content-substitution and denial of service on a shared connection,
not a data read. The same class exists on the `/claim` 413 branch, which also
returns before reading the body (`framing.txt`).

**Why it survived review.** The builder's B1 fix closed the connection on the
`/waitlist` 413 branch and added the follow-up request as the assertion. The
fix landed on one branch, not on the class: the 405 branch and the `/claim` 413
branch were not in that test's reach.

**Smallest change that clears it.** On every branch that answers before reading
the declared body, either read it or set `self.close_connection = True` (and
send `Connection: close`, as the `/waitlist` 413 branch already does). Then
extend the B1 test to the 405 and `/claim` 413 branches: one connection, an
error response, then a valid request.

## Open findings (owner, review)

1. **4.2.1 framing** — above. Owner: builder. Fix is two lines plus a test.
   No card is created by this session.
2. **16.3.3 security events** — the service logs nothing, and `CLAIMS.md` row
   "Our web server keeps no access log" is published. A counter-only record
   (hour, status class, route; no IP, no address) would meet 16.3.3 without an
   access log, but it is a product-lead row first. Owner: security, then
   product lead, before M2.
3. **The `X-Forwarded-For` contract is pinned to Caddy 2.6.2.** The rate-limit
   key is `split(",")[0]` (`app.py:850`). Caddy 2.6.2 sets the header to the
   peer and ignores a client value because `trusted_proxies` is unset
   (`reverseproxy.go:124–129,711–729`); a later Caddy with a trusted proxy
   would prepend the client's value and make 4.1.3 and 2.4.1 fail silently.
   Owner: security; check on any Caddy upgrade, with the two-hop probe in
   `caddy-xff.txt`.
4. **Waitlist token at rest.** 96 bits, stored raw (`app.py:889–894`), and it
   is the bearer for the "you're on the list" page. Not an authentication
   factor, so ASVS 6.5 does not apply; hashing it costs one line and one lookup
   change. Owner: builder, low.

## Residual risk

| Risk | Owner | Review |
|---|---|---|
| The app layer is not measured; SEV-SNP proves the boot, not this code. A host or a memory bug reads every email and token. | security | at M1, with the tenant handoff audit |
| No security-event log (finding 2): a 429/422 storm is invisible today. | security | before M2 |
