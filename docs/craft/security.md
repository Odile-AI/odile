# Security — craft playbook

**Role contract:** [`ownership and collaboration`](../ops/OWNERSHIP-AND-COLLABORATION.md#security) — what this seat owns and the handoffs it owes, calibrated against top-startup job descriptions.

The product is an access claim. Security review is the act of checking whether the
evidence still proves it — and saying so before the user finds the gap.

## Principles

1. **Threat model first.** Name the assets (disk, memory, prompts, keys,
   metadata), the adversaries (host, operator, network, tenant), the boundaries
   and what each party can do. A checklist without a model is theatre.
2. **No standing credentials.** No operator SSH key, no root password, no disk
   passphrase escrow, no shared account. Support is a time-boxed grant with an
   expiry, a forced command and a log.
3. **Verify, don’t trust.** Attestation is checked against a measurement you
   rebuilt; signatures against a published key; hashes against a manifest. A
   green check with no root of trust is decoration.
4. **Least privilege, fail closed.** A process gets the capability it needs and
   nothing else. An auth failure denies; it does not fall through.
5. **Secrets never in git, logs or process lists.** Read them at runtime from a
   0600 file; redact them in errors; never pass them as command-line arguments.
6. **Name the limits.** Measured boot covers firmware, kernel, initramfs and
   cmdline; it does not measure the app layer. The host runs the console. Odile
   can stop the machine and control DNS. State these before anyone asks.
7. **Defense in depth, but count the layers.** LUKS at rest, SEV-SNP in memory,
   TLS in transit, TEE at inference. Each layer has a different adversary; do not
   let one layer’s claim stand in for another’s.
8. **Reproducibility is evidence.** A build you cannot reproduce is a build you
   cannot verify. Record commits, hashes, toolchain versions and the exact
   command.
9. **Prefer removal to mitigation.** The strongest fix for an operator key is no
   operator key, not a policy that says it is rarely used.
10. **Write the residual risk.** Every design leaves something; say what, who
    accepts it, and when it will be revisited.
11. **Frame the connection, not just the request.** Any branch that answers
    before reading the declared body — 405 on an unknown path, 413 on an
    oversized one — must drain the body or close the connection. A proxy pools
    upstream connections, so one client's leftover bytes are parsed as the next
    visitor's request: measured on the live site as `GET /` → 501
    `Unsupported method ('abcGET')`. RFC 9112 §9.3 makes it a MUST.
12. **A gate must fail closed.** A rule whose configuration file is missing is
    not a rule: a `--forbid-file` that silently skips when its file is absent
    passes the exact leak it was built for. A missing allowlist, a missing forbid
    list and an unread scanner must each be a finding, never a skip.
13. **A gate that is not in the local `make check` is a CI-only gate.** The
    commit hook cannot stop the leak, `main` can sit red for days while every
    local gate is green, and the only thing between the tree and the public
    history is the push. Count the layer before you claim it: read the Makefile,
    not the runbook.
14. **A class that is compiled and never applied is worse than a missing one.**
    `ROOT_PATH` read as coverage for `/root/…` in the docstring and was dead
    code; the operator's checkout path sat in the published runbook and the gate
    passed it. A class is real when a planted positive fails the gate.
15. **Audit the verdict as a leak surface.** A public verdict quotes the values
    its drill proves the gate rejects. Either the two probe rows are allowlisted
    with a reason, or the transcript is elided and the digests no longer verify.
16. **A sentence about a guard names the shapes the guard reads.** A runbook
    said the egress check "fails if a packet filter appears here" while the check
    read `command -v nft`; an `iptables` filter left the internet leg green and
    the sentence false. Widen the check where it is cheap, then narrow every
    sentence — heading, bad message, verdict row — to the shapes it actually
    proves, and name the residual. The fence pins the narrow phrase and forbids
    the wide one (`assertNotIn`), so a later edit cannot quietly widen it back.
17. **A tree publishes its paths as well as its bytes.** A gate that opens each
    file and reads its contents has not read the file's name, and a scrub that
    rewrites text never renames. A file called `servury_reseller_key_<id>.txt`
    shipped with clean bytes while both scanners said PASS, because no class
    matched the deny list against a path. Match the list against every relative
    path and archive member name — before the file is opened — and against every
    path each commit carries in history, so a name deleted before the push is
    still a finding. The class is distinct (`forbidden-path`), so an allowlist
    row names the shape it waives and a contents row cannot waive a name.
18. **A report binds one request, and only through `REPORT_DATA`.** The 64 bytes
    at 0x50 are the only field the requester chooses and the firmware signs
    without interpreting (AMD SNP ABI 56860 rev. 1.58 §7.3, Table 23); the
    measurement is always the launch digest, `REPORT_ID` persists for the
    guest's life, and the JSON's `fetched_at` is written beside the signed
    bytes, not inside them. A report refreshed on the machine's own timer is
    genuine and current to the machine, and indistinguishable from a replay to
    the visitor. Either let the caller supply the nonce and say so, or say which
    timer the report answers; never let "fresh" stand for "answers you".

## Exemplars to study

- **OWASP ASVS 5.0** — a verifiable application-security standard; use the
  level appropriate to the asset, not the marketing level.
- **NIST SSDF (SP 800-218)** — secure software development practices, from
  design to release.
- **SLSA** — build provenance and levels; a concrete answer to “what built
  this?”
- **Sigstore / cosign** — keyless signing and transparency logs.
- **AMD SEV-SNP specification** — the actual attestation report, policy bits and
  VCEK chain; read the spec, not a blog.
- **LUKS2 / cryptsetup docs** — keyslots, tokens, header backups; one keyslot at
  handoff is a testable claim.
- **Trail of Bits** and **Google Project Zero** — audit reports and root-cause
  writeups; study how they find the bug, not just the finding.
- **CISA Secure by Design** — defaults, least privilege, logging, secure
  updates.

## Checklist for every security review

- [ ] Assets, adversaries and boundaries are named.
- [ ] `authorized_keys` shows exactly the expected keys; no operator key.
- [ ] `passwd -S root` is `L` or `NP`; SSH password auth is off.
- [ ] One LUKS keyslot at handoff; no keyslot Odile knows.
- [ ] `/proc/cmdline` contains no operator key; the measurement matches the
      published release.
- [ ] `/attestation` verifies against the AMD chain; the freshness model is
      named: `REPORT_DATA` is the caller's nonce, or the page says it is the
      machine's label and gives the refresh interval.
- [ ] Every branch that answers without reading the declared body closes the
      connection (or drains it); probe with an error request then a valid one on
      the same keep-alive connection.
- [ ] A key taken from a proxy header — rate limit, audit, allowlist — is one
      the client cannot write; a two-hop probe shows the proxy overwrites it.
- [ ] Support grant has an expiry, a forced command, a log and a revoke path.
- [ ] No secret in git, logs, process lists or error messages.
- [ ] The publication gate fails when its own config is absent; every allowlist
      row names the artefact class of the value it pins.
- [ ] The gate runs in the local `make check`, not only in CI, and every class
      has a planted positive that fails it.
- [ ] The deny list is matched against paths and archive member names, not only
      file contents; a planted path fails the gate and a stand-in path passes.
- [ ] Every public sentence maps to a `CLAIMS.md` row; no contradiction.
- [ ] Residual risks are written, with an owner and a review date.

## Anti-patterns

- “Zero-knowledge”, “unhackable”, “trustless”, “military-grade”.
- An operator key “for emergencies”; a shared root password; passphrase escrow.
- A support grant without expiry, or a grant that adds a keyslot.
- A claim that the app layer is hardware-measured.
- A signature check with no pinned key or no chain.
- A secret passed in `argv`, echoed in a log, or committed “temporarily”.
- An allowlist row whose reason describes a different value from the one it pins.
- A scanner class skipped because its configuration file is missing.
- A publication gate that reads a file's contents and never its name.
- A “fresh” report whose `REPORT_DATA` the verifier did not choose, with an
  unsigned `fetched_at` offered as the proof.
- A guard's sentence that claims more shapes than the guard reads ("fails if a
  packet filter appears" over a `command -v nft` test).
- A threat model that lists only external attackers and forgets the host.
- A finding with no reproduction command.

## Sources

- OWASP ASVS — https://owasp.org/www-project-application-security-verification-standard/
- OWASP ASVS 5.0, L2 requirement text — https://github.com/OWASP/ASVS/tree/master/5.0/en
- RFC 9112, HTTP/1.1 §9.3 (persistence and the unread body) —
  https://www.rfc-editor.org/rfc/rfc9112.html#section-9.3
- Caddy 2.6.2 `reverse_proxy`, `addForwardedHeaders` —
  https://github.com/caddyserver/caddy/blob/v2.6.2/modules/caddyhttp/reverseproxy/reverseproxy.go
- NIST SSDF — https://csrc.nist.gov/projects/ssdf
- SLSA — https://slsa.dev/
- Sigstore — https://www.sigstore.dev/
- AMD SEV-SNP — https://www.amd.com/en/developer/sev.html
- AMD SEV-SNP ABI Specification 56860 rev. 1.58, §4.3 and §7.3, Tables 9,
  22–24 — https://www.amd.com/content/dam/amd/en/documents/developer/56860.pdf
- cryptsetup / LUKS2 — https://gitlab.com/cryptsetup/cryptsetup
- Trail of Bits — https://blog.trailofbits.com/
- Google Project Zero — https://googleprojectzero.blogspot.com/
- CISA Secure by Design — https://www.cisa.gov/securebydesign
