# Contributing

## Before you start

You need `/usr/bin/python3` (3.11 or later), `make`, `shellcheck` and
`openssl 3.5`. The full gate additionally uses `restic`, `caddy`, `postfix`,
`debootstrap`, `qemu-utils` and `sqlite3`, because the tests exercise the units
that run on a machine.

```sh
make hooks     # enable the committed pre-commit gate
make check     # prereqs, secret scan, publication scan, tests, self-test,
               # compile, lint, claims, brand, roadmap
```

`make check` is the gate. A pull request is ready when it is green on a clean
clone (`make fresh-check`) and the change is named in the commit subject.

## The leak gates

There is one deny list of private machine literals, `docs/ops/leak-forbid.txt`.
It is HQ-only: `docs/ops/` is excluded from git and CI installs the same bytes
from the `LEAK_FORBID_B64` repository secret. The repository publishes no copy,
so a clone has no list — and says so:

```
$ make leak
leak: forbid list absent: docs/ops/leak-forbid.txt — the forbidden-value and forbidden-path classes did not run
forbid list:     absent (docs/ops/leak-forbid.txt) — the forbidden-value and forbidden-path classes did not run
```

Put the list at that path (HQ `cp`, or `base64 -d` from the secret) to arm the
local gates. `make hooks` installs two of them: the pre-commit gate (`make
check`) and the pre-push gate, which scans the outgoing diff and commit messages
against the same list and blocks on a hit. On a clone with no list the pre-push
hook prints `forbid list absent` and lets the push through, because there is
nothing to scan against. `git commit --no-verify` and `git push --no-verify` are
the deliberate bypasses; CI holds the list as a secret and runs it over the tree
and over every blob, so a bypassed literal is still a red run.

Never write a real address, account id, box id, tenant slug, mailbox or
checkout path. Use the placeholder vocabulary:

| Value | Placeholder |
|---|---|
| an IPv4 address | `192.0.2.0/24`, `198.51.100.0/24` or `203.0.113.0/24` (RFC 5737) |
| an IPv6 address | `2001:db8::/32` (RFC 3849) |
| a vendor account id | `<servury-account:dev>`, `<servury-account:host>`, `<servury-account:main>` |
| a machine | `<server:ukitest>` |
| a tenant slug | `odl-000000` |
| any other private value | `<what-it-is>` |

Every private host value lives in `deploy/hosts.local` (gitignored), with
`deploy/hosts.example` as the committed template; `tools/hosts_file.py` reads
either. Real values live in the untracked file or the secret store (SOPS/age,
1Password, Vault), never in git.

The one exception is a live constant a program reads — the demo tenant's slug
`status/status.py` excludes, the boot command line `deploy/image/tenant-image.pin`
records. It stays the real value at its own path and gets a path-pinned row in
`leak-allow.tsv` (`forbidden-value` + that path + the value's digest + why);
anywhere else the same value is still a finding, and the published build scrubs
it to the stand-in. Files that do not read the value get the placeholder, never
a row.

## Style

- Python: standard library only where the tree already is; no new dependency
  without a reason in the commit message.
- Shell: `set -euo pipefail`, quoted expansions, and `shellcheck` clean.
- Every new claim about the product goes in `docs/CLAIMS.md` first, then in the
  surface that makes it; `scripts/claims_lint.py` fails on a claim that is not
  backed.
- Write the test with the change. A fix without a test is a rumour.

## Commits and pull requests

- One subject line: `component: what changed and why`, imperative, under 72
  characters.
- The body names the evidence: the command you ran and what it printed.
- Keep a pull request to one concern. If it touches the image and the website,
  it is two pull requests.

## Reporting a security issue

See [SECURITY.md](SECURITY.md). Do not open a public issue for a live
vulnerability.

## License

By contributing you agree that your contribution is licensed under the MIT
license in [LICENSE](LICENSE).
