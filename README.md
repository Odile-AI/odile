# Odile

Odile gives you a personal Linux computer with an AI agent living inside it, reachable from any browser. Each machine runs on an AMD SEV-SNP confidential VM whose boot is measured, so a stranger can check what is running without trusting us. One agent per subscriber, provisioned from a signed image, updated in place, and still working when the tab is closed.

## Demo

A live demo workspace runs at <https://odile.fyi/judge> — sign in with the public demo password, ask the agent to write a file, close the tab, and the file is still there.

## Demo account

| Field | Value |
|---|---|
| Username | `judge` |
| Password | `odile-demo-kestrel-2026` |

The password is public by design: one shared workspace on Odile's own tenant machine.

## Core components

- **`app.py`** — the workspace server: sessions, files, jobs, and the terminal, in front of a Hermes agent.
- **`odilectl/`** — the configuration and claim CLI that binds a machine to its owner.
- **`installer/`** — the signed installer that provisions a fresh machine and verifies its own bytes.
- **`release/`** — the signed release manifest, the public signing key, and the published installer.
- **`seed/`, `status/`, `update/`, `support/`** — first-visit seeding, the status surface, in-place updates, and the support agent.
- **`attest.py`, `odile-verify`, `odile-verify.pin`** — the attestation report, the standalone verifier a stranger runs, and the measurement it checks against.
- **`deploy/`** — systemd units, Caddy, Postfix and OpenDKIM configuration, and the boot-image build.
- **`web/`, `templates/`, `skills/`** — the static site, the workspace templates, and the agent skills shipped with a machine.
- **`demo/`** — a local demo that runs on one standard-library server, with no network and no key.
- **`scripts/`, `tools/`** — the operator scripts that run on a machine (install, backup, manifest, recovery kit) and the build tools that run on the tree (claims and brand lint, the secret and leak scanners, the image bench).
- **`tests/`** — the suite `make check` runs.

## License

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
