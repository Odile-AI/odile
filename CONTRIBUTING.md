# Contributing

## Before you start

You need `/usr/bin/python3` (3.11 or later), `make`, `shellcheck` and
`openssl 3.5`. The full gate additionally uses `restic`, `caddy`, `postfix`,
`debootstrap`, `qemu-utils` and `sqlite3`, because the tests exercise the units
that run on a machine.

```sh
make hooks     # enable the committed pre-commit gate
make check     # prereqs, secret scan, tests, compile, lint, claims
```

`make check` is the gate. A pull request is ready when it is green on a clean
clone (`make fresh-check`) and the change is named in the commit subject.

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
