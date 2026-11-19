# The leak gate — Security's verdict

**Card:** ODL-906 (`t_4060be11`) · **Date:** 2026-11-15 · **Seat:** Security
**Mechanism:** `93a73a7` (scanner, allowlist, builder), `baf8d00` (Makefile, CI,
hardening), `24ee323` (the tree cut once), and the publication at
2026-11-14T14:54:56Z (repository recreated 16:52:19Z).
**Tree reviewed:** local `main` `1876e99` plus this card — 318 files,
9,339,996 bytes, 103 allowlisted values, `tools/leak_scan.py --tree` **PASS**.
`main` is 36 commits ahead of `origin/main` `55ec2c1` (311 files, 74 allowlisted,
PASS), so this is a review of the **next** publish.
**Status:** **published.** Round 1 could say "every fix is free". It cannot now:
a value pushed to `origin/main` is in the history forever, and the only thing
that keeps it out is the gate on the push — which is why §5 matters more than it
did.

The question is not "does the gate pass?" — it does. It is "would it stop a
leak, and is every exception it grants true?". The first answer is *not always*;
the second is *for every row now, after this card's amendments*.

## Verdict, per claim

| # | Claim | Verdict | Smallest change that clears it |
|---|---|---|---|
| C1 | The class list fits what Odile actually holds | **block** | Add the classes in §1; several of the values are already in the tree, so this is not speculative |
| C2 | Every allowlist row is true | **block** | Five rows misdescribed their value; reasons amended here, three structural defects carded (§2) |
| C3 | The strategy is sound | **pass, amended** | Keep value scoping and the frozen pair; fix expiry, binary coverage, and the gate's own tests (§3) |
| C4 | The platform layer is complete | **block** | `harden_repo.sh` reads two of the four scanning fields it sets; eight controls off (two scanning fields, `enforce_admins`, signatures, the tag ruleset, CodeQL, the Actions policy, retention), several of them org-level and now possible (§4) |
| C5 | The gate fails when it cannot run | **block** | The `forbid` class is dead (no list in the tree, a missing list is silent) **and the gate is not in `make check`**, so `main` was already red on it (§5) |
| C6 | The drill passes | **pass** | Transcript in §5 — but the card's `make leak` target no longer exists |

## 1. The class list — what is missing, and what is over-broad

Eleven classes exist and are a good generic set. They are still not derived from
what Odile holds. Every row below was measured on the tree this card reviews,
which the gate passes clean:

| Missing class | Value in the tree | Why it is a leak | Proof |
|---|---|---|---|
| **Hardware identity** | full SEV-SNP reports in `tests/fixtures/verify/*.json`; the chip id is report offset `0x1A0` | a silicon id is the strongest machine fingerprint there is; three fixtures carry the **HQ** chip id `967acf92…`, and one carries the tenant's `3fa18b1e…` | `raw[0x1A0:0x1E0].hex()` on the decoded `report_b64` → `967acf92…`; `/var/lib/odile/attestation.json` on this host carries the same prefix |
| | `tests/fixtures/verify/tenant-vcek.der` (deleted by `t_3cd62ef9`) | VCEK serial + HWID extension are the same chip in DER; the scanner at this head read only printable runs | `leak_scan.scan_bytes(open(...,'rb').read())` → `[]` at that head; the scanner now decodes DER and PEM certificates and reports a serial only when the AMD HWID extension is present |
| | `docs/runbooks/image-bench.md:387` PARTUUID | a real volume's id | gate `[]`; `partuuid` is on the self-blinding suppression list |
| **Vendor resource ids** | `31a63bbb` (server) in 17 files, `oqwaclzm` (host) 5, `odl-7971a8` (tenant slug) 25, `24b35c62` 2 | the research record itself says GitHub-only scanning fails on "our vendor ids"; the forbid list names only the retired box | `grep -rEl` over the tree; gate PASS |
| **DNS resource ids** | `tests/test_dns.py:32` `ZONE_ID = "39068d33…"` | a Cloudflare zone id names the account's zone; the `cloudflare` token rule wants 37 hex, zone ids are 32 | gate `[]`; P19 |
| **SSH host-key fingerprints** | `support.md:203,205,470,588,716`, `install.md:923,951,1253,1325`, `image.md:730`, `web/verify.html:172-173` | a host-key fingerprint identifies a machine | gate `[]` — every line matching `sha(256\|512)` suppresses high-entropy, so the `SHA256:` class blinds itself; P04 |
| **`/root/…` paths** | `docs/runbooks/deploy.md:69` carried the operator's checkout path until this commit | `ROOT_PATH` is **compiled at `tools/leak_scan.py:145` and never applied** — the docstring's "`/root/…` or `/home/<name>/…`" is half-implemented | the forbid list's `checkout-path` found it in one run (`FINDING forbidden-value docs/runbooks/deploy.md:69`); the gate itself never did |
| **A credential that is not an assignment** | `README.md:16` publishes the demo password in a table cell | the demo password is a real credential to a live machine (§2 row 2); the class only sees `NAME=value` | P20 (a table row carrying the password) passes the gate |
| **IP hidden in a hostname** | the product's own live URL shape | dashes bypass the dotted-quad regex | P02 |
| **MAC / machine-id / serial** | `aa:bb:…`, a 32-hex machine-id, a chassis serial | hardware identity by another name | P05, P06, P21 |
| **macOS / Windows home paths** | `/Users/<name>/…`, `C:\Users\…` | a contributor's name is a person | P07 |
| **Passphrase / mnemonic / seed names** | `LUKS_PASSPHRASE=…`, `MNEMONIC=…`, `SECRET_KEY=…` | `CRED_NAME` ends at SECRET/TOKEN/PASSWORD/…; `PASSPHRASE` and `SECRET_KEY` match none, and a no-digit passphrase fails entropy | P08 |
| **Opaque payloads** | a `.tar.gz` member, DER, base64 > 128, UTF-16 | printable-run extraction cannot see inside | P10, P11 |
| **Commit messages, branch and tag names** | `git log --format=%B` | permanent, as public as a file | history mode reads blobs only: a commit subject carrying an address and a mailbox scanned `PASS` |
| **National phone formats** | `(415) 555-2671` | the phone class is E.164 only | P16 |

**Over-broad — fix these or the gate gets switched off in irritation:**

- CGNAT/Tailscale addresses (RFC 6598) are "public". P13 fails the gate.
- A version quadruple is an IPv4. P14 fails the gate.
- `data:image…base64,` and `data:font…base64,` skip **the whole line**, not just
  the high-entropy rule: an address or mailbox on a font line is invisible. P15.
- The `sha(256|512)|digest|checksum|partuuid` suppression is too wide: it is
  there to stop reporting hashes and it also stops reporting fingerprints.
- `leak-allow.tsv`'s header says "There is no wildcard rule", but **9 of 21**
  rows use `*` (rows 7, 11–18). They are value-pinned, so it is not a hole — it
  is a contradiction that misleads the next reviewer. The sentence to keep is
  `tools/leak_scan.py:39`'s: "no wildcard rule, and no path-wide row" is what
  the *code* does not enforce either.

**Derived from what Odile holds, the class list should be:** private-key,
provider-token, credential-assignment (with a passphrase/mnemonic arm **and a
bare-credential arm for published passwords**), high-entropy,
**machine-identity** (chip id, VCEK serial, PARTUUID, machine-id, MAC, hardware
serial), **vendor-resource-id** (server, box, account, tenant slug, host name),
**dns-resource-id** (zone and record ids), **ssh-fingerprint** (exempt from the
sha suppression), public-ip (including dotted IPs inside hostnames),
public-ipv6, email, home-path (**including `/root/…`**, which is compiled and
never used), macOS/Windows home paths, basic-auth-url, phone,
**archive-member** scanning, and forbidden-value.

## 2. The allowlist, row by row

Reviewed against the code and the live machine, not against the reasons. 21 rows
after this card (18 at the reviewed head), all live on 2027-12-31, all matched at
least once (103 allowlisted values in the tree). The card said 22 rows and two debt rows;
`93a73a7` created 22, `75a21aa` removed the four address rows by re-cutting the
signed pair with TEST-NET-3, and this card adds three: one fixture value the boot
test needs, and two for the drill transcript below.

| # | Rule · path | Value actually pinned | Verdict |
|---|---|---|---|
| 1 | public-ip · `scripts/send_test.py` | five copies of an APNIC-allocated address (not TEST-NET-3) | **disagree: reason was false.** It is not a documentation address and not a recipient — it is an SPF `ip4:` token and a DNS A-record fixture inside `--self-test`. Reason amended here; the real fix is to use a TEST-NET-3 value and delete the row (F1). |
| 2 | credential-assignment · `deploy/demo-host/login.html` | the published demo password | **agree** — and the reason is now the measured one. With it the Webapp shell runs as `tenant` (uid 1000), `sudo -n id -un` returns **root**, and `/home/tenant/.hermes/.env` (0600 `tenant`) carries a live Near AI key. The demo host is **not** the retired HQ copy; it is the tenant VM `odl-7971a8` (see §6). Copies: `README.md:16` (a table, invisible to every class) and `deploy/demo-host/apply_demo_public.py:1645,1648`, plus this page's `var DEMO_PASSWORD`. |
| 3 | credential-assignment · `deploy/image/tenant-image.pin` | the one-time seed setup token | **agree** — reason true as of 2026-11-14T14:54:27Z. Live probe `POST https://odile.fyi/seed` with the pin's token and `bb.iid` → **HTTP 410 `{"error":"token already used","reason":"used"}`**; the pin's `loose_measurement` is `sev-snp-measure … --append "$CMDLINE"` (`deploy/image/uki.sh:116-118`), so the token cannot be scrubbed without re-cutting the measured cmdline. |
| 4 | credential-assignment · `tests/test_first_visit.py` | `a-different-long-password-0002` | **disagree: reason inverted.** The reset **accepts and stores** it (`test_the_reset_works_and_rotates_the_code` asserts `verify_password(NEW_PASSWORD, stored_hash)` true). Reason amended. |
| 5 | credential-assignment · same | base64 of `secret-placeholder` | **agree.** Decodes exactly; a fixture session secret. |
| 6 | credential-assignment · same | `backup-password-…` | **agree.** Only interpolated into the recovery text; no restic or openssl call uses it. |
| 7 | high-entropy · `*` | `key=<b64>` of a public SSH key | **agree.** Decodes to `ssh-ed25519 … odile-bench throwaway boot key`; no private half in the tree. Shape caveat: the pin's `bb.key=` is longer than `MAX_TOKEN` and invisible. |
| 8 | high-entropy · `tests/test_first_visit.py` | `HERMES_DASHBOARD_BASIC_AUTH_SECRET=<b64>` | **disagree.** Reason said "an environment-variable name, not a value"; the match is name **and** value, and the value is row 5 again. The defect is the scanner: `TOKEN`'s charset includes `=`, so it swallows `NAME=`. Reason amended; the scanner fix is F3. |
| 9 | high-entropy · `tests/test_secret_scan.py` | a 43-char literal | **disagree.** The test asserts `secret_scan.allowlisted(...)` is **True** — that the *other* scanner does not report it. No test asserts `leak_scan` reports anything (there is no test of `leak_scan` at all). Reason amended. |
| 10 | private-key · `tests/test_claim.py` | the redacted PEM block | **agree.** The marker pair is the fixture the endpoint must reject; a real key there changes the digest and is a finding. |
| 11 | email · `*` | a generic Gmail address × 11 | **agree, reworded.** A fixture recipient in the DSN and message tests; it is a real provider address, so "placeholder" was generous. |
| 12 | email · `*` | Google's DMARC aggregate mailbox | **agree.** The `From:` of the fixture. |
| 13 | email · `*` | `dmarc-1@google.com` | **disagree.** A **Message-Id right-hand side**, not a reporting mailbox. Reason amended. |
| 14 | email · `*` | `odile-backup@backup.example` | **agree.** `.example` is reserved; it is an SSH user string, not a mailbox. |
| 15 | email · `*` | `odile-no-such-user-…@gmail.com` | **agree** as far as provable. Deliberate non-existence cannot be re-proven without probing Gmail. |
| 16 | email · `*` | port25's verifier | **agree.** A public authentication checker quoted in the email runbook. |
| 17 | home-path · `*` | `/home/tenant/` × 51 | **agree, re-cut.** It is the tenant account the product creates (uid 1000) — the demo machine's **real** home, not "a generic account a benchmark names". Reason amended. |
| 18 | home-path · `*` | `/home/odiletest387/` × 5 | **agree.** A throwaway simulation account under `/var/tmp`. |
| 19 | credential-assignment · `tests/test_first_visit.py` | base64 of `not-the-image-secret` | **agree (added by this card).** The value arrived with the ODL-521 merge and made `main` red on its own gate (`FINDING … :360`); it is the bench image's placeholder session secret and the boot test asserts it survives. |
| 20 | public-ip · `docs/security/LEAK-CONTROL-VERDICT.md` | the drill's probe address | **agree (added by this card).** The card's Definition of Done requires the drill transcript pasted; the transcript carries the probe values it proves the gate rejects. Path-scoped, so another address in the doc is still a finding. |
| 21 | email · same | the drill's probe mailbox | **agree (added by this card).** Same row pair; the mailbox is a deliberate non-existent address at a real provider. |

**Structural defects in the allowlist as a record** (carded, not fixed here):

- **One expiry date for everything.** 21 rows all expire 2027-12-31. On
  2028-01-01 the gate fails on values that are still correct, and the header
  forbids moving a date without a reason — the path of least resistance is a bulk
  bump, which is the permanent baseline the design rejected. Constants (published
  password, public key, redaction marker, reserved domain) should not expire at
  all; debt should carry the date of the card that clears it.
- **A live credential can be waived silently.** Nothing in the format says "this
  row covers a credential". Row 3 did exactly that for three weeks.
- **Rows 17 and 18 are self-satisfying** (the reason text contains the value), so
  "every row matched" is not proof a row is still needed.

## 3. The strategy

- **Value-scoped rows, not path-wide: pass.** The digest pins the matched value;
  a different address at the same path is still a finding. This is the right fix
  for the failure that let a fake key sit in a test.
- **Expiry instead of a permanent baseline: pass in principle, amend in
  practice** — see the single-date cliff above.
- **Binaries by printable runs: pass for ELF strings, not enough.** Compressed
  members, DER, base64 runs > 128 and UTF-16 are invisible (P10, P11, the VCEK).
  Add archive-member extraction and a decode-then-scan step for long base64.
- **Freezing `release/` and `installer/` instead of rewriting signed bytes:
  pass.** Re-signing to scrub a byte breaks the hash the installer embeds and the
  detached signature over the pair. The consequence is that the frozen files are
  never scrubbed, so the gate — not the builder — must prove them clean. With
  the forbid class off (§5) that proof does not exist.
- **A gate with no tests: block.** `grep -rl leak_scan tests/` → **0 files**;
  the 24 tests in `test_secret_scan.py` cover `secret_scan.py`. The drill is its
  only exercise, and the card's drill target (`make leak`) is gone.
- **A gate that runs on its own scanner: block.** CI executes
  `tools/leak_scan.py` and `leak-allow.tsv` from the PR's checkout, so a PR that
  weakens either passes its own gate. Run the base-branch copy against the PR
  tree, or put both paths in CODEOWNERS.
- **Where a leak still gets through — confirmed and extended:** the card names
  PR/issue text, workflow logs and a fork's own branch. Confirmed, plus: commit
  messages, tag and branch names (history mode reads blobs only); release assets
  and their names; Actions artifacts and caches (public, 90 days); the repository
  description and website field; code-search and Copilot indexing of a fork;
  `git notes`; **and the local path to the publish: `make check` never runs this
  gate, so the commit hook cannot stop a leak either** (§5).

## 4. The platform layer

> **Re-measured and closed by ODL-906-F4 on 2026-11-15.** The table below is the
> state at the review; what the platform reads now, row by row, is §11.

Measured on the real repository (`Odile-AI/odile`), 2026-11-15T02:45Z, read-only:

| Control | State |
|---|---|
| secret scanning | on |
| push protection | on |
| secret scanning **non-provider patterns** | **disabled** (patched, answered 200, stayed off) |
| secret scanning **validity checks** | **disabled** (same) |
| private vulnerability reporting | on |
| Dependabot alerts + automated fixes | on |
| default workflow permissions | read (cannot approve PR reviews) |
| branch protection | five required checks, strict, force-push and deletion blocked |
| `enforce_admins` | **false** — an org owner bypasses every check on `main` |
| required signed commits | **false** |
| rulesets (tag protection) | **none** (`[]`; no tags exist yet) |
| CodeQL default setup | **not-configured** |
| Actions | `allowed_actions: all`, `sha_pinning_required: false` |
| artifact + log retention | 90 days (the maximum) |
| fork-PR approval | **on, weakest policy** — `actions/permissions/fork-pr-contributor-approval` → `{"approval_policy":"first_time_contributors"}`: a first-time contributor needs approval, a returning contributor's workflows run. `all` is the strict setting. (Round 2 read a 404 from `…/fork-pr`, a route that does not exist.) |
| repo/webhooks/deploy keys | 1 repo, 0 webhooks, 0 deploy keys |
| collaborators | two accounts: the agent (`member`) and the owner |
| org policy | `Odile-AI` is an **Organization** (free): members may create, delete and change the visibility of repositories |

What this means:

1. **`harden_repo.sh`'s verify block still lies by omission.** It PATCHes
   `secret_scanning_non_provider_patterns` and `secret_scanning_validity_checks`,
   GitHub answers 200 and leaves both `disabled`, and the verify step reads only
   `secret_scanning` and `secret_scanning_push_protection` — then prints "every
   control is on". Measured true on the live repo, not only on a throwaway.
2. **No repository ruleset and no tag rule.** Any writer, or a compromised
   workflow token with `contents: write`, can create, move or delete a release
   tag. Tags are immutable by convention only.
3. **No required signed commits.** A "published from a fresh root commit"
   provenance claim rests on nothing cryptographic.
4. **No CodeQL default setup.** One `PATCH code-scanning/default-setup` turns it
   on and is free here; without it the code half of a leak is unscanned.
5. **Artifacts and logs are public for 90 days** and actions are unpinned by SHA
   — a supply-chain hole in a repo whose workflows otherwise pin every action.
6. **The org now exists, so the "structural" gaps are choices.** Round 1 wrote
   "no org-wide push-protection policy is structural because `savio-agent` is a
   User"; `Odile-AI` has since been created as an Organization. An org-wide
   push-protection policy, an org ruleset and org-level SHA pinning are now
   available and unset.
7. **A push-protection bypass is one click for any writer**, and nothing narrows
   who may use it. `enforce_admins: false` means the owner can also push to
   `main` past all five checks.
8. **Fork-PR approval is on, at the weakest setting.** A first-time
   contributor's workflow waits for approval; a returning contributor's runs
   without one. `all` is the strict setting and is one `PATCH` away — the right
   choice for a public repository that takes patches. The org-level default is
   not readable with a member token (`403`). The round-2 row read a `404` from
   `…/fork-pr`, a route that does not exist: a 404 is ambiguous between *no such
   setting* and *no such route*, so a platform row is only as good as the
   endpoint it was read from.

## 5. The drill, the probes, and the two ways the gate does not run

The card's drill says `make leak`. **There is no `leak` target in the Makefile
any more** — the gate moved into CI (`publish-lint`) and out of `make check`:

```console
$ printf 'host 45.33.32.156\nmail real.person@fastmail.com\n' > probe.md && make leak
make: *** No rule to make target 'leak'.  Stop.
```

The same drill against the entry point that exists (FAIL, then PASS):

```console
$ printf 'host 45.33.32.156\nmail real.person@fastmail.com\n' > probe.md
$ /usr/bin/python3 tools/leak_scan.py --tree
leak scan: tree
  files scanned:  319
  bytes scanned:  9,340,044
  allowlisted:    103
FINDING public-ip              probe.md:1 sha256:6c4a9f5f0de6 len=12
FINDING email                  probe.md:2 sha256:8d38531c89cb len=24  (fastmail.com)
FAIL: 2 finding(s) in the tree
$ rm probe.md && /usr/bin/python3 tools/leak_scan.py --tree
leak scan: tree
  files scanned:  318
  bytes scanned:  9,339,996
  allowlisted:    103
PASS: nothing to leak in 318 files, 9,339,996 bytes scanned
```

(The byte count is the run's own; a later re-cut of this page moves it. The file
count and the two findings are the evidence.)

The card's Definition of Done says **in a public-tree checkout**. `make leak` is
absent there too; the published gate at `origin/main` `55ec2c1`
(a real `git clone https://github.com/Odile-AI/odile`) gives the same verdict:

```console
$ printf 'host 45.33.32.156\nmail real.person@fastmail.com\n' > probe.md && make leak
make: *** No rule to make target 'leak'.  Stop.                       # exit 2
$ /usr/bin/python3 tools/leak_scan.py --tree
leak scan: tree
  files scanned:  312
  bytes scanned:  8,899,225
  allowlisted:    74
FINDING public-ip              probe.md:1 sha256:6c4a9f5f0de6 len=12
FINDING email                  probe.md:2 sha256:8d38531c89cb len=24  (fastmail.com)
FAIL: 2 finding(s) in the tree                                        # exit 1
$ rm probe.md && /usr/bin/python3 tools/leak_scan.py --tree
leak scan: tree
  files scanned:  311
  bytes scanned:  8,899,177
  allowlisted:    74
PASS: nothing to leak in 311 files, 8,899,177 bytes scanned           # exit 0
```

**Fail-open #1 — the forbid class is dead.** There is no `leak-forbid.txt` in
the tree (the list lives only in the private `docs/ops/`), the default
`--forbid-file leak-forbid.txt` never resolves, and a missing file is a silent
skip — for the default *and* for an explicitly named file:

```console
$ printf 'vendor box <box-id> account <account-id>\n' > probe-forbid.txt   # 37 bytes, ids elided
$ /usr/bin/python3 tools/leak_scan.py --tree
leak scan: tree
  files scanned:  319
  bytes scanned:  9,340,033
  allowlisted:    103
PASS: nothing to leak in 319 files, 9,340,033 bytes scanned      # exit 0
$ /usr/bin/python3 tools/leak_scan.py --tree --forbid-file /nonexistent-forbid.txt
PASS: nothing to leak in 319 files, 9,340,033 bytes scanned      # exit 0
$ /usr/bin/python3 tools/leak_scan.py --tree --forbid-file <checkout>/docs/ops/leak-forbid.txt
FINDING forbidden-value        probe-forbid.txt:1 sha256:3e5275784e44 len=8  (servury-box-id)
FINDING forbidden-value        probe-forbid.txt:1 sha256:4014bb9b4e56 len=8  (servury-dev-account)
FAIL: 2 finding(s) in the tree
# the same list on the base 1876e99, its own allowlist, before this branch's deploy.md fix:
FINDING forbidden-value        docs/runbooks/deploy.md:69 sha256:18b74e4c8911 len=11  (checkout-path)
FINDING forbidden-value        probe-forbid.txt:1 sha256:3e5275784e44 len=8  (servury-box-id)
FINDING forbidden-value        probe-forbid.txt:1 sha256:4014bb9b4e56 len=8  (servury-dev-account)
FINDING credential-assignment  tests/test_first_visit.py:360 sha256:7d6c1d64963a len=28
FAIL: 4 finding(s) in the tree   (318 files, 9,304,714 bytes, 89 allowlisted)
```

The prohibited literals are elided here on purpose: this page is in the tree the
forbid list is meant to police, so writing them out would make the verdict the
next finding. The labels and the digests are the evidence.

That `checkout-path` finding is the point: the tree this card reviews **carried the
operator's checkout path at `docs/runbooks/deploy.md:69`**, and the only class
that can see it is the one that never runs. The tree is fixed in this commit
(the operator's checkout path → `<checkout>`); the class stays off until F1 lands.

**Fail-open #2 — the gate is not in `make check`.** `docs/ops/LEAK-CONTROL.md`
§Layers says "in the public tree `check` includes `make leak`". At this head the
Makefile has no leak reference at all, so `.githooks/pre-commit` (`make check`)
cannot stop a leak, and `main` was **red on its own gate** until this card's row
19: `FINDING credential-assignment tests/test_first_visit.py:360` — a finding
nobody saw because nothing local runs the scanner. The leak is stopped only at
the push, by CI.

Probes the gate passes today (one file each, in this tree):

| Probe | Content | Gate |
|---|---|---|
| P02 | an address with dashes in a hostname | pass |
| P04 | an `SHA256:` host-key fingerprint line | pass |
| P05 | a MAC address | pass |
| P06 | a 32-hex machine-id | pass |
| P07 | `/Users/<name>/…` | pass |
| P08 | `LUKS_PASSPHRASE=…` | pass |
| P09 | 64-hex on a line naming sha256 | pass |
| P10 | UTF-16LE mailbox | pass |
| P11 | `.tar.gz` holding an address and a mailbox | pass |
| P15 | `data:image/png;base64,…` line with both | pass |
| P16 | a national-format phone | pass |
| P19 | a 32-hex zone id | pass |
| P20 | the published password in a Markdown table row | pass |
| P21 | a hardware serial | pass |
| P13 | CGNAT (100.64/10) | **FAIL** — over-broad |
| P14 | a version quadruple | **FAIL** — over-broad |

History mode works for its stated case — a file committed and then deleted was
found (`FINDING public-ip commit 674906c gone.txt:1`) — and does not read commit
messages: a commit subject carrying the same address and mailbox scanned
`PASS: nothing to leak in 2 commits`. The published repository itself (15
commits, one root, no merges) is clean **with the forbid class on**: tree and
history PASS, 311 files / 339 blobs.

## 6. What still leaks — the sentence

**The public tree publishes the demo password that gives any stranger a root
capable shell on Odile's tenant machine `odl-7971a8` and reads that tenant's live
Near AI key; a spent seed setup token, the tenant slug, the vendor server, host
and account ids, the tenant's silicon chip id with its VCEK serial and the HQ
chip id in three fixtures, a real disk's PARTUUID, a Cloudflare zone id, several
SSH host-key fingerprints and the private tree's commit sha; and the gate still
cannot see a `/root/…` path, a hardware serial, a MAC, a machine-id, an IP
dashed into a hostname, a fingerprint on a `SHA256:` line, a password in a table
cell, an archive member, a base64 blob longer than 128 characters, a UTF-16
payload, a commit message, a tag or branch name, a PR body, a workflow log, an
artifact, or anything a PR adds to the scanner or the allowlist it is judged
by.**

Everything before the second semicolon is a finding on this card; everything
after it is a hole in the mechanism.

**The demo host, measured (2026-11-15T00:44–00:47Z).** `demo.odile.fyi` is
Odile's tenant machine, not the retired HQ copy — `/verify` serves measurement
`9f01725a…` and chip `3fa18b1e…`, matching the tree's `tenant-2026-11-12.json`
fixture and `docs/runbooks/verify.md`. Signed in with the **published** password
over the tenant's own Webapp console:

```console
$ id -un; id -u; sudo -n id -un; hostname
tenant
1000
root
odile-bench
$ stat -c "%a %U %G" /home/tenant/.hermes/.env
600 tenant tenant
$ cut -d= -f1 /home/tenant/.hermes/.env | grep -v '^#'      # names only
HERMES_DASHBOARD_BASIC_AUTH_USERNAME
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH
HERMES_DASHBOARD_BASIC_AUTH_SECRET
HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY                     # len 35, live
$ systemctl is-active odile-meter; systemctl is-enabled odile-meter
inactive
not-found
$ ls -ld /var/lib/odile; command -v nft || echo no-nft
drwxr-xr-x 2 root root 4096 Nov 14 04:51 /var/lib/odile
no-nft
$ systemctl list-timers --all | grep -iE 'reset|nightly' || echo no-reset-timer
no-reset-timer
$ cat /proc/cmdline | tr ' ' '\n' | grep -E '^bb\.'
bb.release=2026.11.2
bb.host=odl-7971a8
bb.iid=31a63bbb
bb.ip4=<the live tenant address>/24
bb.ip6=<the vendor /64>:1/64
```

So the blast radius of the published password is: root on Odile's tenant
machine, every judge's sessions, the machine's attestation files, and the live
inference key — which the demo shell spends against Odile's shared balance. The
meter is now **removed** (unit not-found, no user, `/var/lib/odile` root-owned),
not "masked" as measured on 2026-11-14; there is no packet filter and no nightly
reset. That is a bigger blast radius than "one shared workspace", and the two
public sentences that still say the demo is *not* a tenant machine are stale:
`docs/product/FIRST-RUN-SIX-ACTS.md:267` and `docs/product/CLAIMS-RECUT-STRICT.md:73`.
`docs/CLAIMS.md` §1, `web/verify.html:118` and `tests/test_flow.py:555-560` are
already re-cut and correct.

## 7. What must happen before the next publish

1. **Get the gate into `make check`** (and therefore the pre-commit hook). Today
   `main` can be — and was — red on its own gate while `make check` is green.
   *Block; new card.*
2. **Turn the forbid class on and make it fail closed**: ship `leak-forbid.txt`
   in the tree, exit 2 on a named-but-missing file, and add the ids actually in
   use (`31a63bbb`, `oqwaclzm`, `odl-7971a8`, `24b35c62`). *F1.*
3. **Add the missing classes** in §1's order, machine identity first and
   `/root/…` immediately — it is compiled and never used. *F3.*
4. **Fix the platform verify block** and add a tag ruleset, required signatures,
   CodeQL default setup, org-level push protection, and SHA pinning. *F4.*
5. **Re-cut the two stale product sentences** that still say the demo is not a
   tenant machine, and record the demo host's blast radius where a judge can read
   it. *Product lead (with t_781fc5cb).*
6. Decide, as an owner call, whether the attestation fixtures ship raw (signed by
   AMD, therefore not redactable without breaking the chain the tests verify) or
   are replaced by a synthetic report with a synthetic KDS. *Savio.*

## 8. Residual risks, written down

- **The demo password is a standing root credential to a live, reachable
  machine.** It is public by design (CLAIMS §1) and now measured: `sudo -n` →
  root, the tenant's live inference key readable at 0600. The design is
  deliberate (`deploy/demo-host/check_demo_host.sh:288-296` asserts it), it has
  no rotation date, and its owner is Savio under the demo decision (ODL-503).
  Revisit at demo retirement or at the first tenancy that shares the key.
- `release/` and `installer/` are frozen and therefore never scrubbed. They are
  clean today; only the forbid class can keep them clean, and it is off.
- Push protection is the last line against a provider token and is bypassable by
  any writer with one click; the owner can also push past all five checks.
- The scanner is the *only* gate that knows about addresses and ids; CI runs it
  from the PR's own checkout, so a PR that weakens `tools/leak_scan.py` or
  `leak-allow.tsv` passes its own gate.
- A leak already in `origin/main`'s history stays there; today's published
  history is clean with the forbid class on, and every future publish re-runs it.

## 9. Follow-up cards

Gated on this card:

- `t_e7010764` — **ODL-906-F1**: wire the forbid list into the tree, fail
  closed, and name the ids actually in use. *(Now the top of the list.)*
- `t_eeda9549` — **ODL-906-F2**: the pin's token reason is true; the token is
  spent (410), so what is left is the wording, already amended here.
- `t_ee35d75f` — **ODL-906-F3**: add the missing classes (machine identity and
  `/root/…` first) and fix the over-broad and self-blinding rules.
- `t_7bef55d3` — **ODL-906-F4**: finish the platform layer, and run the gate on
  its own scanner rather than the PR's.
- `t_6f2ebc48` — **ODL-906-F5**: the publication gate is not in `make check`;
  `main` was red on it while the local gate was green.
- `t_292108d5` — **ODL-906-F6**: the allowlist's record — one expiry for
  everything, no credential marker, two self-satisfying rows.

One decision is Savio's, not a card's: whether the attestation fixtures ship raw
(signed, therefore not redactable) or are replaced by a synthetic report with a
synthetic KDS chain. It trades test fidelity against publishing a silicon id.

## 10. Rounds 1 and 2's findings, at this head

| Item | Status here |
|---|---|
| Must 1 — row 2's blast radius was the retired HQ copy | **fixed.** Reason, §6 and §8 re-cut with the tenant facts measured above; the demo host is `odl-7971a8`, the shell is `tenant`, `sudo` reaches root, the live Near AI key is readable, the meter is removed, there is no filter and no reset. The two stale public sentences are named in §6. |
| Round-2 must — the fork-PR row read a 404 as "no control" | **fixed.** `…/actions/permissions/fork-pr` is not a route (404); `…/fork-pr-contributor-approval` answers `{"approval_policy":"first_time_contributors"}`. The control is on at its weakest setting; the count is **eight**, not nine. A platform row is only as good as the endpoint it was read from. |
| Must 2 — no acceptance stamp | run after this commit; the stamp names this head. The card's body names two private `docs/ops/` files, so the stamp is taken from a worktree that carries them untracked-and-excluded; the public tree cannot carry them. |
| Nit 1 — `4869bbe`/`75a21aa` row history | corrected: `93a73a7` created 22; `75a21aa` removed the four debt rows; this card adds three (19–21). |
| Nit 2 — `31a63bbb` in 16 files | corrected: 15 at `87a0f75`; **19** here, **17** excluding this page and the allowlist. |
| Nit 3 — the sslip value is not in the built tree | corrected: it is a probe (P02), not a tree value. |
| Nit 4 — 9 not 8 `*` rows; the sentence lives in the scanner | corrected: 9 of 21 here (rows 7, 11–18); the header's claim is the contradiction. |
| Nit 5 — the password has four copies | corrected: **three files** now (`README.md`, `login.html`, `apply_demo_public.py` ×2); `web/judge.html`, `DEMO-THREAT-NOTE.md` and `tests/test_flow.py` copies are gone. |
| C5 — the token blocked publication | closed: spent 2026-11-14T14:54:27Z, 29 s before the repository went public; live probe 410. |
| `make leak` | gone from the Makefile; the gate is CI-only. §5. |

## 11. §4 re-measured and closed (ODL-906-F4, 2026-11-15)

The platform layer was rebuilt as a script that sets every control and then
**reads every field it sets back**. It was run twice: first on a throwaway
public repository, then on `Odile-AI/odile` with `--profile=public-open`; both
were re-measured at 07:48Z after the round-2 review. The values below are the
live repository, read directly from the API (not from the script's own output).
The script's own table on the throwaway was 37 ok / 3 gaps / 0 failures; the
independent read-back on the same repository was 33 ok / 4 gaps over 37 rows
(its extra gap is the `advanced_security` row it carries separately). On the
live repository the script reads 37 ok / 10 gaps / 0 failures and the
independent read-back 34 ok / 10 gaps over 44 rows. The throwaway was deleted
(`GET` → 404, `public_repos` → 0).

| Control | Endpoint | Measured |
|---|---|---|
| secret scanning | `GET repos/Odile-AI/odile` | `enabled` |
| push protection | same | `enabled` |
| secret scanning non-provider patterns | same | `disabled` — **gap, plan** |
| secret scanning validity checks | same | `disabled` — **gap, plan** |
| GHAS (`advanced_security`) | same | absent — why the two rows above cannot be turned on |
| private vulnerability reporting | `GET …/private-vulnerability-reporting` | `true` |
| Dependabot alerts | `GET …/vulnerability-alerts` | `204` |
| Dependabot security updates | `GET repos/…` | `enabled` |
| default workflow permissions | `GET …/actions/permissions/workflow` | `read`, cannot approve PR reviews |
| merge shape | `GET repos/…` | squash only, no merge commit, no rebase, delete branch on merge, update branch; `has_wiki`/`has_projects` false |
| branch protection | `GET …/branches/main/protection` | five contexts (`check`, `publish-lint`, `history-secrets`, `fresh-check`, `demo-check`), strict, no force push, no deletion, conversation resolution required |
| `enforce_admins` | same | `false` — **by design** (`public-open`: the publishing account is a repository admin and pushes `main` directly; binding admins would stop every publish) |
| required PR reviews | same | `null` — same design |
| required signed commits (main) | same | `false` — **gap, deferred** (below) |
| Actions policy | `GET …/actions/permissions` | `enabled: true`, `allowed_actions: selected`, `sha_pinning_required: true` |
| Actions allowlist | `GET …/actions/permissions/selected-actions` | only `actions/checkout@*` and `github/codeql-action@*` |
| artifact + log retention | `GET …/actions/permissions/artifact-and-log-retention` | `7` days (was 90) |
| fork-PR approval | `GET …/actions/permissions/fork-pr-contributor-approval` | `all_external_contributors` (was `first_time_contributors`) |
| CodeQL default setup | `GET …/code-scanning/default-setup` | `configured` (was `not-configured`), `query_suite: default` |
| tag ruleset | `GET …/rulesets/27297820` | `target: tag`, `active`, conditions `include: ~ALL`, `exclude: []`; rules `deletion`, `update`, `non_fast_forward`, `required_signatures` |
| org plan | `GET orgs/Odile-AI` | `free` |
| org push-protection policy | `GET orgs/Odile-AI/code-security/configurations` | **403** — member token |
| org ruleset | `GET orgs/Odile-AI/rulesets` | **404** — member token |
| org Actions policy / SHA pinning / fork-PR default | `GET orgs/Odile-AI/actions/permissions*` | **403** — member token |
| members may delete repositories | `GET orgs/Odile-AI` | `true` — the publisher can delete the repository (the purge path) |
| members may change visibility | same | `true` — the publisher can make it private |

**Corrections to §4, measured while closing it.**

1. **`all` is not a value.** `PUT …/fork-pr-contributor-approval` with
   `{"approval_policy":"all"}` answers **422**: the accepted values are
   `first_time_contributors_new_to_github`, `first_time_contributors` and
   **`all_external_contributors`**. The strict setting is the last one, and it
   is now set. The §4 sentence "`all` is the strict setting" named a value the
   API rejects.
2. **The two scanning fields are a plan limit, not a broken PATCH.** On a
   user-owned public repository and on the free org, the same `200` leaves
   `secret_scanning_non_provider_patterns` and
   `secret_scanning_validity_checks` at `disabled`. GitHub's own docs put both
   behind GitHub Secret Protection: generic patterns and validity checks are
   listed for "organization-owned repositories on GitHub Team with GitHub Secret
   Protection enabled". `advanced_security` is absent from
   `security_and_analysis`, which is the machine-readable form of the same fact.
   `tools/harden_repo.sh` now prints them as named `gap`s and refuses to say
   "every control is on" while they are off.
3. **Signed commits on main: measured, deferred, and the signing path proven.**
   A branch ruleset with `required_signatures` refuses an unsigned push
   (`GH013`, "Commits must have verified signatures"), and the rule checks
   **every commit the push introduces** — an unsigned ancestor blocks a signed
   tip. Today's open branches carry commits made before signing was configured,
   so enabling the rule now would reject their merges. The signing path itself
   works: an SSH signing key is registered on the publishing account and a
   signed commit is `verified: true` at the API. The tag ruleset already carries
   `required_signatures`; a clean positive control on a throwaway accepted a tag
   push that introduced only a signed commit (local `%G?` = `G`, API
   `verified=true reason=valid`) and refused a tag push that introduced an
   unsigned commit, and one whose signed tip sat on an unsigned ancestor. The
   enable step that remains is owned by **`t_476c7a6f`** (parented on ODL-231,
   the last known unsigned branch) — not a missing mechanism.
4. **The tag ruleset is real.** Deleting a tag answers `GH013 "Cannot delete
   this tag"`; moving one answers `GH013 "Cannot update this protected ref"` /
   `"Cannot force-push to this tag"`. A tag placed on a commit already on the
   remote is not re-checked (the rule reads the objects the push introduces), so
   the signature half becomes universal only with signed commits on main. Round
   1's transcript mis-labelled `v0.0.11` as "a tag at a signed commit accepted";
   it was `rc=1` at unsigned ancestor `99b52bb`. The round-2 drill
   (`docs/ops/evidence/ODL-906-F4/tag-ruleset.txt`) carries a clean acceptance
   case instead: `v0.0.1` on a signed commit introduced by that push, `rc=0`.
5. **The gate no longer runs the scanner it is gating.** `publish-lint` used to
   execute `tools/leak_scan.py` and `leak-allow.tsv` from the PR's checkout; a
   PR that added an early `return 0` and an allowlist row for its own leaked
   address passed its own gate (measured: `rc 0`). The job now fetches the base
   branch and runs **its** scanner with **its** allowlist over the PR tree,
   then the tree's own scanner. The same drilled PR now fails on the base pass
   (`FINDING public-ip README.md:43`, `rc 1` at base `d5c63fb`). On a push the
   base is `github.event.before`, not `HEAD^` (which is the previous new commit
   on a multi-commit push); `tests/test_ci_gate_scanner.py` executes the step's
   own shell against a fixture tree rather than asserting YAML text.

## 7. Follow-up, 19 November 2026 — the signed fixtures are synthetic

**Card:** ODL-906-F3 follow-up (`t_3cd62ef9`) · **Seat:** Security

The four recorded SEV-SNP reports and the AMD-issued `tenant-vcek.der` are
gone. `tools/make_synthetic_report.py` builds a 1184-byte report and a local
P-384 VCEK/ASK/ARK chain; the fixtures carry an `ODILE-SYNTHETIC-…` chip id and
name their own test-only ARK in a `trust` block. The shipped `odile-verify.pin`
keeps AMD's ARK, and the trust block is honoured only for a local `--report`
file — a document served over the network cannot choose its root.

The three `machine-identity` allowlist rows are deleted. The PARTUUID row stays
until the benchmark record is re-cut with a placeholder command line.

**The fifth identity the F3 verdict missed.** `tests/fixtures/amd/vcek.pem` was
the demo chip's real AMD certificate: its HWID extension carried the same HQ chip
id, and the scanner did not see it because it read DER, not a PEM certificate's
base64 body. The scanner now decodes a PEM `CERTIFICATE` block and reads a
certificate's serial only when the AMD HWID extension is present (a bare serial
is not a VCEK). The real VCEK was removed; the verifier fetches the report's own
VCEK from AMD's KDS on the default live run (a local `--amd-dir` that binds the
report skips the fetch).

**Evidence.**

- `tools/leak_scan.py --tree` → `PASS: nothing to leak in 370 files`, 55 allowlist rows (4 credentials,
  0 debt), with the three rows gone. The worktree has no private `leak-forbid.txt`, so the
  forbidden-value class only runs in the built tree below.
- The built public tree at `9f5c857` (373 files) → `PASS: nothing to leak in
  367 files`, 221 allowlisted, `forbid list: leak-forbid.txt (14 labels)`.
- `tests/test_leak_scan.py::AllowlistRecord::test_a_planted_chip_id_in_a_fixture_fails_the_gate`
  plants a real 64-byte chip id in a fixture: the gate fails with
  `machine-identity`, and the same fixture with the `ODILE-SYNTHETIC-` marker
  passes.
- `tests/test_leak_scan.py::AllowlistRecord::test_a_pem_certificate_with_a_live_chip_id_fails_the_gate`
  plants the same id in a PEM certificate: the gate fails; a PEM certificate
  without the HWID extension passes.
- `tests/test_verify.py` runs the verifier signature and chain checks on the
  synthetic chain, the loopback-KDS tenant path, and a served `trust` block
  that is ignored.

**Residual.** `docs/runbooks/image-bench.md` still carries one real PARTUUID
inside a recorded measured command line (the existing row). The tip is clean;
the history and already-published commits still carry the old recorded reports,
because this card replaces the fixture set, not the history.
