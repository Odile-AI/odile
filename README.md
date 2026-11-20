# Odile

Full agent with full privacy.

Odile is a privacy-friendly AI agent that gets things done for you. Every Odile agent runs on its own AMD SEV-SNP confidential virtual machine with encrypted memory and [NEAR AI](https://near.ai/) confidential model inference within hardware enclaves, enabling full confidentiality throughout agentic workflows.

## Demo

Live demo: https://demo.odile.fyi/login

The live demo is a shared Odile instance showcasing how could Odile looks and works. Odile the project is in active development, stay tuned for its first release.

The demo instance's hardware attestation report is available at https://demo.odile.fyi/verify. It prints the one-line verifier command with the measurement it serves.

## Core components

- **`app.py`** — the workspace server: sessions, files, jobs, and the terminal, in front of a Hermes agent.
- **`odilectl/`** — the configuration and claim CLI that binds a machine to its owner.
- **`installer/`** — the signed installer that provisions a fresh machine and verifies its own bytes.
- **`release/`** — the signed release manifest, the public signing key, and the published installer.
- **`seed/`, `status/`, `update/`, `support/`** — first-visit seeding, the status surface, in-place updates, and the support agent.
- **`attest.py`, `odile-verify`, `odile-verify.pin`** — the TEE attestation report, the standalone verifier a stranger runs, and the measurement it checks against.
- **`deploy/`** — systemd units, Caddy, Postfix and OpenDKIM configuration, and the boot-image build.
- **`web/`, `templates/`, `skills/`** — the static site, the workspace templates, and the agent skills shipped with a machine.
- **`demo/`** — a local demo that runs on one standard-library server, with no network and no key.
- **`scripts/`, `tools/`** — the operator scripts that run on a machine (install, backup, manifest, recovery kit) and the build tools that run on the tree (claims and brand lint, the secret and leak scanners, the image bench).
- **`tests/`** — the suite `make check` runs.

## License

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
