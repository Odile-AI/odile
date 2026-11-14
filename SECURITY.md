# Security policy

## Reporting a vulnerability

Email **security@odile.fyi** with the affected component, the version or commit,
and the smallest reproduction you have. Do not open a public issue for a
vulnerability that is still live. We acknowledge within three working days.

## What is in scope

- **Attestation** — `attest.py`, `odile-verify`, `odile-verify.pin`, and the
  report a running machine serves. A report that verifies but does not describe
  the running machine is the highest-severity class here.
- **The installer** — `installer/` and `release/`. It verifies its own bytes
  against a detached signature before it runs; a way to make it install
  something else is in scope.
- **The workspace** — `app.py` and the surfaces it serves: sessions, files,
  jobs, terminal, and the claim flow.
- **The image** — `deploy/image/`. Measured boot, the sealed initrd, and the
  first-boot path.

## What is out of scope

- The demo workspace. Its password is published on purpose and it is not a
  tenant machine.
- Denial of service by volume against a machine you do not own.
- Findings that require an already-compromised host or an operator key.

## Disclosure

We publish the fix and the credit together, after the affected machines have the
update. If you need a deadline, say so in the report and we will agree one.
