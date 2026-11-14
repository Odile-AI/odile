# Odile

A full agent, privately.

Odile is an AI agent that gets things done with privacy. Every Odile agent runs on its own AMD SEV-SNP confidential virtual machine and uses confidential inference, protecting your agent from outsider access at the hardware level.

## Demo

Live demo: https://demo.odile.fyi/login

The machine's own report is at <https://demo.odile.fyi/verify>. It prints the one-line verifier command with the measurement it serves; a tenant run needs `--expect`.

## How NEAR fits in

Inference runs on NEAR AI's confidential cloud, inside a hardware enclave.
In this demo your prompts go from the browser to Odile's tenant machine and then to the NEAR AI enclave.

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
