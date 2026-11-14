# The installer — one signed command, run by the user

ODL-203. `installer/odile_install.py` turns a freshly booted Debian VM into a
working Odile tenant. The **user** runs it as root; Odile never logs in, holds no
operator key and never sees the sign-in password.

The tenant install is one extraction: the scrypt hash, the `.env` allowlist, the
managed-block config merge, the atomic writes, the Hermes install, the Webapp
build and the service handling live in the installer. The demo tenant
(`odl-7971a8`) runs this same signed installer, so the demo and a customer
tenant cannot drift in the parts that matter.

What the installer cannot prove is the machine's own legs — the measured command
line, the initramfs encrypt/unlock, DNS/ACME against the real host and the
issued certificate. Those are ODL-332; the simulation stops at the loopback gate
(§9).

## 1. Committed files

| File | Role |
|---|---|
| `installer/odile_install.py` | the installer: one printable, standard-library program (it carries the tenant backup, first-visit and verify sources, the tenant templates and the tenant bundle) |
| `installer/odile_install.py.sig` | detached Ed25519 signature, base64; made with the **CI-held** release key by the release workflow's `make release-sign` (§7) |
| `installer/embed_backup.py` | refreshes the embedded tenant backup copy and its pinned SHA-256; the only supported way to change it (ODL-215 follow-up) |
| `installer/odile-signing-key.pub` | the **release** public key: it signs this installer, the manifest, `release.json` and the seed certificate; the same PEM travels in the recovery kit and is published at <https://odile.fyi/odile-signing-key.pub> |
| `installer/odile-signing-key.prev.pub` | the release key being retired, shipped for one release; the seed certificate may be verified against it during a rotation (ODL-514) |
| `deploy/seed/seed-key.pub` | the **seed** public key: the key that signs tenant seeds, certified by the release key |
| `deploy/seed/seed-key.crt` | the release key's 64-byte Ed25519 signature over `seed-key.pub` (ODL-514) |
| `installer/embed_tenant.py` | refreshes the embedded standing context and first-run skills, with their pinned SHA-256; the only supported way to change them |
| `installer/sim_tenant.py` | the machine-free tenant simulation and its assertions (§6) |
| `templates/config.yaml` | the tenant config template the installer embeds byte-for-byte; `--self-test` fails if the two differ |
| `templates/skin.yaml` | the Odile Webapp skin the installer embeds and writes to the tenant |
| `templates/webapp-brand.js` | the Webapp brand script the installer embeds and links from the built shell |
| `docs/runbooks/install.md` | this page |

The recovery kit is a JSON file the user receives with the machine:

```json
{
  "slug": "odl-7f3a2c",
  "instance_id": "srv-…",
  "setup_token": "<64 lowercase hex, single use, 24 h>",
  "signing_public_key": "-----BEGIN PUBLIC KEY-----\n…",
  "signing_key_sha256": "<64 lowercase hex: sha256sum of odile-signing-key.pub>"
}
```

`setup_token` is the seed exchange credential (ODL-204,
[`seed.md`](seed.md)); `signing_public_key` is the trust anchor for both the
installer and the seed response — the release key, whose certificate over the
seed key is what the seed response carries. (The plain-text kit the user
receives also prints the seed public key and that certificate; the JSON above
is the installer's input.) `signing_key_sha256` names the exact file that key
must be: the installer prints the digest of the key it is using and exits 3
before the seed exchange when the kit names a different one. Nothing in the kit
is a password or a private key.

## 2. Modes

| Command | What it does | Changes anything? |
|---|---|---|
| `./installer/odile_install.py --self-test` | tests the pure functions: scrypt format, `.env` allowlist, config merge, the embedded tenant bundle's pins, Caddy splice, gate shape, recovery rendering, seed validation, signature, signing-key fingerprint, the boot decision and its record, heap sizing, readiness | no |
| `--verify-signature --pubkey <pem>` | prints the key's file SHA-256 and `SHA256:` fingerprint, then checks this file against a detached signature | no |
| `--print-source` / `--manifest` | prints the whole program or the static manifest (JSON) for audit | no |
| `--dry-run --recovery-kit <json>` | prints the plan; verifies nothing, writes nothing, starts nothing | no |
| `--recovery-kit <json>` (as root) | the tenant install (§3) | yes |
| `--root <dir> --user <u> --port <p>` | the machine-free simulation, as an unprivileged user (§6) | only under `<dir>` |

Tenant defaults: user `odile`, Webapp `127.0.0.1:9120`,
`public_url https://<slug>.odile.fyi`.

The tenant install also takes the boot assertion: `--expect <96-hex>` (the
measurement the verifier printed), optionally `--release <id>` to assert the
release id, and `--boot-report PATH` when the report is not at the tenant
default `/run/blackbox/attest.json`. §3a is the contract. The nightly backup
posts its signed heartbeat to `https://odile.fyi/heartbeat` (the receiver's
route; `backup.md` §2); `--heartbeat-url` names another receiver.

`--dry-run` prints the exact `name=version` pins the package step would install
and the dated snapshot they come from (§6.1); `--manifest` prints the same table,
the snapshot and this file's sha256, so a release and an installed machine can be
compared field by field.

## 3. The tenant ritual

The user runs, on the fresh machine, as root:

```sh
# 1. put the three files and the kit on the machine (scp, or the installer image)
# 2. check the signature before running anything
./odile_install.py --verify-signature --pubkey odile-signing-key.pub
# 3. the out-of-band check: is that the key Odile published?
curl -s https://odile.fyi/odile-signing-key.pub | sha256sum

# 3. the install; --expect is the 96-hex number the verifier printed
./odile_install.py --recovery-kit recovery-kit.json --expect <96-hex>
```

1. **Signature first, against a key you checked.** The installer prints
   `signing key  sha256 <hex> · SHA256:<base64>` before the `signature` result.
   `<hex>` must equal the `sha256sum` above and the `signing_key_sha256` in the
   recovery kit; `<base64>` is the same key in the spelling the manifest and
   update tools print. Only then does the installer check its own bytes against
   the kit's public key, before the first state change. A one-byte edit fails
   (§7); a kit naming a different key stops it with exit 3, before the seed
   exchange — a bundle and key swapped together would otherwise verify.
2. **Boot check.** It reads the boot report `bb-seed` copied at switch-root
   (`/run/blackbox/attest.json`), compares its signed measurement with
   `--expect`, and stops before the seed exchange on any disagreement (§3a).
   It prints the release, the measurement and the boot-key fingerprint.
3. **Seed exchange.** It POSTs `{token, instance_id}` to
   <https://odile.fyi/seed> and verifies the response through the seed
   certificate: the release key whose digest it printed (the kit's
   `signing_public_key`) certifies the seed key the body carries, and that seed
   key signs the body (§8). It caches the seed at `/etc/odile/seed.json` (0600)
   plus `/etc/odile/seed.sig`. A re-run uses the cache and sends nothing;
   rotating the token is an `odilectl` job (ODL-204).
4. **Odile sign-in.** It asks for *your Odile sign-in* password — 16+
   characters, or blank to generate 32 and print it once. It is hashed on the
   machine with scrypt (`scrypt$n$r$p$salt$dk`, the dashboard provider's own
   format) and only the hash reaches `.env`. HQ never sees it.
5. **Install.** OS packages at their exact Debian 13 pins from one dated
   snapshot (git, Caddy, Node, restic, tmux, curl, openssl, Python; Chromium
   best-effort — §6.1), the pinned Hermes commit `2bd378ed1b8d` (v0.23.0) as
   `odile`, the Webapp bundle, the skills,
   the machine's own NEAR AI key from the seed (`/etc/odile/key.env`), the units,
   the nightly backup program and its timer (ODL-215 follow-up), the
   per-slug Caddy site. The Hermes step first fetches
   `install.sh`, checks its sha256 against `HERMES_INSTALL_SHA256` and refuses
   before `bash` on a mismatch (§4b); only then does the commit pin apply.
6. **Lock the doors.** `passwd -l root`, `PasswordAuthentication no` in
   `/etc/ssh/sshd_config.d/99-odile.conf`, `odile` with no password and
   NOPASSWD sudo (D12, so `sudo apt install` inside the agent's terminal works).
7. **DNS and ACME.** It waits for `<slug>.odile.fyi` to resolve and retries
   ACME until a certificate is issued (15 minutes each), then prints the
   certificate's SHA-256 fingerprint.
8. **Recovery file.** `/etc/odile/recovery.txt` (0600) holds the backup
   password and a certificate-fingerprint placeholder. The first-visit page
   downloads the completed file beside the recovery code, and the service fills
   the fingerprint from the certificate the browser is using and writes it back
   to this file; the file stays on the machine as the agent fallback. The
   installer prints the file's path and refuses to exit without the exact phrase
   `I have copied it off the machine` (or `--i-have-copied-it` when driven
   non-interactively).
9. **Smoke.** The sign-in gate (302/401/200), one chat turn that runs a shell
   command and writes a file, and one cron job that runs. The cron job is
   `odile-smoke`, every 5 minutes, no agent, delivering locally.

### 3a. The boot check (ODL-396)

PIN-POLICY §5 puts one assertion at install time: the machine the installer is
about to write a secret to is the boot the user verified. The installer reads
the file Servury's `bb-seed` copies at switch-root — never `/dev/sev-guest`:

```json
{"version": 1, "release": "2026.11.1", "status": "ok",
 "hostkey": "ssh-ed25519 …", "fingerprint": "SHA256:…",
 "report_b64": "<base64 of the 1184-byte SEV-SNP report>",
 "cmdline": "… bb.release=2026.11.1 bb.ip4=… bb.key=… …"}
```

It runs **after** `--verify-signature` and **before** the seed exchange. On a
mismatch it exits **5** with `no seed was exchanged and no secret was written`;
the setup token stays unspent and `/etc/odile/seed.json` is not created. On a
tenant with no boot record, `--expect` is required: without it the installer
exits **2** (`no --expect and no boot record`) instead of installing
unasserted. `--boot-report` defaults to `/run/blackbox/attest.json` and must be
readable by root.

The checks, in order:

| Check | Failure |
|---|---|
| `status == "ok"` | exit 5 |
| `report_b64` decodes to 1184 bytes and the report version is 5 | exit 5 |
| the measurement at `0x90:0xC0` equals `--expect` (96 hex) | exit 5 |
| the command line is non-empty and carries exactly one `bb.key=` and one `bb.ip4=` | exit 5 |
| when the kit carries `boot_key_fingerprint`, the decoded `bb.key` has that SHA-256 fingerprint | exit 5 |
| `--release ID`, when given, equals the report's `release` | exit 5 |

`bb.key` is base64 of the OpenSSH public key (a kernel command line cannot
carry spaces); the fingerprint is `ssh-keygen -lf`'s `SHA256:…` over the key
blob. A normal key line's trailing comment (and an `authorized_keys` options
prefix) is not part of the blob and does not change the fingerprint. The
success line is printed before the seed exchange:

```
signature    OK (sha256 …)
boot         release 2026.11.1 · /run/blackbox/attest.json
             measurement <96 hex>
             boot key    SHA256:…
write        /etc/odile/boot-record.json (boot record, release 2026.11.1, 4 vCPU, installer …)
fetch        seed from https://odile.fyi/seed (instance …)
```

On success it writes `/etc/odile/boot-record.json` (0644 root) — the
installer's copy of PIN-POLICY §4.3:

```json
{
  "schema": "odile.boot-record.v1",
  "release": "2026.11.1",
  "measurement": "<96 hex>",
  "cmdline_sha256": "<sha256 of the cmdline with ' initrd=initrd' stripped>",
  "vcpus": 4,
  "checked_at": "2026-11-09T…Z",
  "expected": "<the 96 hex this run was checked against>",
  "installer_sha256": "<this file's sha256>",
  "source": "/run/blackbox/attest.json",
  "artifacts": null
}
```

`artifacts` is null because the pin entry carries the artifact hashes and
measurement parameters; this record is per machine. It is not a secret, and a
root shell can edit it (PIN-POLICY §8).

**A re-run after a release move.** Without `--expect`, the installer uses the
record: the same measurement prints `same boot record …` and writes nothing. A
different measurement exits **5** and says to re-run the verifier and pass the
new `--expect`; passing it replaces the record. The same is true when only the
recorded `cmdline_sha256` or `release` moved. In the simulation a missing
report is not an error: `--root` with no report prints
`skip boot check (simulation)` and continues, so the old harness still runs.

The long steps run under `tmux` when the session is a TTY, so a dropped SSH
session does not abort the install; `--no-tmux` runs inline, and a re-run
rejoins whatever is missing. Every step compares before writing, so a second run
prints `same` and restarts nothing (the smoke turn is not repeated unless
`--force-smoke` is passed; that turn costs money).

## 4. What it writes

| Path | Mode | Owner | Content |
|---|---|---|---|
| `/home/odile` | 0750 | odile | the agent's home; workspace and README |
| `/home/odile/AGENTS.md` | 0644 | odile | the standing tenant context; the gateway's `WorkingDirectory` is `$HOME`, the one place Hermes reads it |
| `/home/odile/.hermes` | 0700 | odile | Hermes home, config, `.env`, skills, scripts |
| `/home/odile/workspace` | 0755 | odile | the user's files; survives a restart |
| `/home/odile/.hermes/skills/<name>/SKILL.md` | 0644 | odile | every embedded skill, starting with `odile-first-run`; a skill that leaves the bundle is removed on the next run |
| `/home/odile/.hermes/.odile-managed-skills.json` | 0644 | odile | the skill names this installer wrote, so a retired skill is removed without touching one the user added |
| `/home/odile/.hermes/config.yaml` | 0644 | odile | five managed blocks: model, agent, custom_providers, webapp, onboarding; Hermes's own keys are preserved; `display.skin: odile` (§4a) |
| `/home/odile/.hermes/skins/odile.yaml` | 0644 | odile | the Odile skin: paper and ink, no accent hue, serif headings (§4a) |
| `<dist-webapp>/odile-brand.js` and the `<script>` tag in `index.html` | 0644 | odile | the four strings a skin cannot reach: title, wordmark, accessible name, About credit (§4a) |
| `/home/odile/.hermes/.env` | 0600 | odile | exactly three keys: the dashboard username, the scrypt hash, the cookie secret |
| `/etc/odile` | 0750 | root:odile-manifest | the directory below; the `odile-manifest` group is the manifest listener's traverse, and that listener is the only non-root reader |
| `/etc/odile/seed.json`, `seed.sig` | 0600 | root | the cached seed and its signature |
| `/etc/odile/boot-record.json` | 0644 | root | the boot the app layer was installed on: release, measurement, cmdline hash, vCPUs, the checked `expected` number, installer sha256 and source (§3a) |
| `/etc/odile/apt-pins.list` | 0644 | root | the only apt source the package step reads: `snapshot.debian.org/archive/debian/<stamp> trixie main` (§6.1) |
| `/etc/odile/key.env` | 0600 | root | the machine's own NEAR AI key, one line `HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY=<key>`; `odile-agent.service` carries `EnvironmentFile=` for it |
| `/etc/odile/recovery.txt` | 0600 | root | backup password + certificate fingerprint |
| `/etc/odile/{support-key.pub,backup-repo.txt}` | 0644 | root | the seed's public material |
| `/usr/local/lib/odile/backup.py` | 0755 | root | the reviewed tenant backup (ODL-215), pinned by SHA-256; reads `/etc/odile/backup-repo.txt` and `/etc/odile/recovery.txt` at run time (ODL-215 follow-up) |
| `/etc/systemd/system/odile-backup-tenant.service` | 0644 | root | the nightly oneshot: one restic snapshot, retention, the status record, and the signed heartbeat |
| `/etc/systemd/system/odile-backup-tenant.timer` | 0644 | root | daily 03:00, `Persistent=true`; enabled and started by the tenant install |
| `/usr/local/bin/odile-install.sh` | 0755 | root | this program's exact bytes, placed right after the signature check and before the boot record, so `--manifest`, `scripts/manifest.py` and CLAIMS §4.6 hash a file that exists (ODL-332 follow-up) |
| `/etc/systemd/system/odile-agent.service` | 0644 | root | gateway + Webapp, `User=odile`, no sandbox (sudo) |
| `/etc/sudoers.d/odile` | 0440 | root | `odile ALL=(ALL) NOPASSWD:ALL`, checked with `visudo -c` |
| `/etc/ssh/sshd_config.d/99-odile.conf` | 0644 | root | key-only SSH, checked with `sshd -t` |
| `/etc/caddy/Caddyfile` | 0644 | root | one block between `# BEGIN odile-<slug>` and `# END odile-<slug>`; validate, timestamped backup, restart, rollback |

No operator SSH key, no root password, no disk passphrase and no sign-in
password are written anywhere except the hash in `.env`. The seed carries the
machine's own NEAR AI key, and the installer writes it to `/etc/odile/key.env`
(0600 root); `odile-agent.service` reads that file through `EnvironmentFile=`, so
the key is in the gateway's process environment at exec, and the profile `.env`
carries no inference key. Inference goes straight to NEAR AI:
`model.provider: custom`, `model.base_url: https://cloud-api.near.ai/v1`, and
one `custom_providers` row named `Near AI` whose `key_env` is
`HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY`. There is no local proxy and no local
monthly cap.

### The tenant bundle

`$HOME/AGENTS.md` and the first-run skills travel inside the signed installer,
pinned by SHA-256 (`--self-test` fails on a one-byte edit). `AGENTS.md` sends a
fresh agent to `odile-first-run` when memory has no `onboarded_at`; the skill
holds the five questions, the brief file and the schedule. The `onboarding`
block keeps Hermes's own first-run chat (scripted greeting and name card) from
opening before Odile's, and `profile_build: "off"` stops the first-message
profile-build offer.

`installer/embed_tenant.py` refreshes the embedded copies from
`templates/tenant-agents.md` and `skills/*/SKILL.md`; adding a skill means
adding its directory and re-running the embed. A skill that leaves the bundle
is removed on the next installer run, using the manifest the installer wrote;
a skill the user added is never in that manifest and is left alone. The examples
that run against a fresh scratch root are in `installer/sim_tenant.py` (§6).

## 4a. The Webapp skin (ODL-240)

The Webapp is Hermes's desktop UI; on a tenant it is a product surface, not an
admin panel. The installer applies Odile's skin and one same-origin brand
script, so nobody edits a file on the machine by hand.

| Source | Written to | Role |
|---|---|---|
| `templates/skin.yaml` | `/home/odile/.hermes/skins/odile.yaml` (0644) | a Hermes skin: paper and ink, no accent hue, serif headings, the developer status bar hidden |
| `templates/config.yaml` | `/home/odile/.hermes/config.yaml` | the `display.skin: odile` block, merged beside the five managed blocks; every other display key is kept |
| `templates/webapp-brand.js` | `<dist-webapp>/odile-brand.js` plus a `<script>` tag in `index.html` | the strings a skin cannot reach: the page title, the empty-state wordmark and its accessible name, the rotating composer prompt, and the About credit line |
| `deploy/image/odile-webapp-brand.py` | `/usr/local/lib/odile/odile-webapp-brand.py` (0755) | the re-assert: the same splice, from the same bytes, whenever the bundle changes |
| `deploy/image/webapp-brand.js` | `/usr/local/lib/odile/webapp-brand.js` (0644) | the bytes the re-assert copies into the bundle, byte-identical to the template |
| `deploy/image/odile-webapp-brand.path` + `.service` | `/etc/systemd/system/` | a path unit on the built shell starts the oneshot; the boot is not the trigger |
| `deploy/image/odile-webapp-brand.conf` | `/etc/systemd/system/odile-agent.service.d/10-odile-webapp-brand.conf` | the backstop: the same helper runs as an `ExecStartPre` on every gateway start |

**Why the script.** Hermes v0.23.0 hard-codes `<title>Hermes</title>`,
`HERMES AGENT` in the empty state, the rotating composer prompt ("Give Hermes
a task", from the bundle's own list) and the About hero into the bundle; a skin
themes the palette, not strings. The script is parse-blocking and same-origin
(no third-party request). It rewrites the composer prompt both at scan time and
on the attribute mutation the composer makes when it remounts, so the first
screen a stranger sees names Odile. It also writes the first-visit appearance key
(`hermes-desktop-theme-v2: odile`) only when the browser has no saved
appearance, so a first visit paints the Odile skin rather than the bundle
default; a later choice in Appearance wins and is never overwritten. Until the
gateway resolves the skin, an `!important` stylesheet holds paper and ink over
the bundle's blue accent and removes itself when the real theme is written. The
skin also settles the vendor's hard-coded emerald success icon to ink, so the
only saturated colour on the surface is the error red. The script guards the
vendor skin's `fontUrl` so no font host is contacted.

**After a rebuild.** `hermes update` and `hermes webapp --build-only` rebuild
`apps/desktop/dist-webapp` from the vendor package, which drops both the asset
and the tag: measured on `demo.odile.fyi` at 2026-11-14 01:26Z, the tab read
`Hermes` and the empty state `HERMES AGENT` while `display.skin: odile` still
held the palette. The image therefore ships the re-assert as part of the
machine, and the trigger is the bundle changing, not a boot
(`odile-webapp-brand.path` watches `index.html` and `odile-brand.js`; the
`ExecStartPre` drop-in covers the update flow that rebuilds and then restarts
the gateway). Both run `odile-webapp-brand.py`, which uses the installer's
bytes and tag format, is idempotent by content (`same` on a second run) and
keeps the owner of the shell it patches. The build also converges the image
itself, so a fresh machine never serves the vendor's name even once.

```sh
$ journalctl -u odile-webapp-brand.service -n 5
  write        Webapp brand (odile-brand.js vf46b92de7db1, index.html linked)
  same         Webapp brand (odile-brand.js vf46b92de7db1, index.html linked)
$ grep -c 'odile-brand.js' \
      /home/odile/.hermes/hermes-agent/apps/desktop/dist-webapp/index.html
1
```

A converged home prints `same` for all three installer steps; a bundle that is
missing is `wait` (the path unit fires when the build creates it), and a shell
without `<head>` or `</html>` is left alone until the build finishes it.

**What is not themed, and why** (ODL-240: list it, leave it):

- **`GET /login`** — the gateway's server-rendered sign-in page: vendor amber
  `#ffac02`, "Nous Research", "Hermes Agent dashboard", and the horse favicon.
  v0.23.0 exposes no template or config seam for it, and replacing it would
  fork the auth page rather than skin it. It is the one screen before the
  product that still looks like the vendor's admin panel; every screen after
  sign-in is Odile's.
- **The favicon and the About brand mark** — vendor assets, kept as the
  "Powered by Hermes" credit.
- **The About hero and the model/effort labels** — vendor copy, kept.
- **The first-run usage-stats consent banner** — "Share usage stats? Counts
  only. Never your messages or files." with "Share with Nous". Vendor copy and
  a vendor consent choice, kept: it is how the open-source core asks before it
  sends anonymous counts. Its colours are the skin's.

**Verify on the machine** (as the user; the launcher path is the pinned
install):

```sh
$ sudo -u odile env HOME=/home/odile HERMES_HOME=/home/odile/.hermes \
      /home/odile/.hermes/hermes-agent/.hermes/bin/hermes skin list
  odile            user     Paper and ink — Odile's quiet Webapp skin
$ sudo -u odile env HOME=/home/odile HERMES_HOME=/home/odile/.hermes \
      /home/odile/.hermes/hermes-agent/.hermes/bin/hermes config get display.skin
odile
$ grep -n 'odile-brand.js' \
      /home/odile/.hermes/hermes-agent/apps/desktop/dist-webapp/index.html
    <script src="./odile-brand.js?v=…"></script>
```

Open the Webapp: the tab reads `Odile`, the empty state reads `Odile`, and
Settings → About shows `Hermes`, `Version <x.y.z>` and `Powered by Hermes`.
`--manifest` carries the skin and brand hashes so an audit can tie the running
files to the signed installer.

**Known limits.** The skin engine clips `customCSS` to 32 KiB, so the embedded
Cormorant face used on the landing page cannot travel inside the skin. The
heading stack is the system serif — `"Iowan Old Style", "Palatino Linotype",
Palatino, Georgia, "DejaVu Serif", serif` — the closest available face, and no
font host is contacted. Dark mode is not in V0.1: the desktop converter
synthesises `darkColors` from the skin's `colors`, so the resolved theme is
paper in both modes and the app stays light.

## 4b. The Hermes install script (ODL-399)

The pinned commit is not the first thing that runs. `install.sh` is fetched from
`https://hermes-agent.nousresearch.com/install.sh` and executed with `bash` as
the tenant **before** the commit pin applies, so a changed script could do
anything on the machine and then check out the reviewed tree. It is the one
third-party program the installer executes.

`HERMES_INSTALL_SHA256` covers it. `ensure_install` fetches the bytes, hashes
them, and refuses before the script is written or `bash` starts when the hash
differs:

```
error: the fetched Hermes install script does not match its pinned hash
       fetched  <sha256> (<bytes> bytes)
       pinned   c73dad4dcf7c9ecadb98578e28cc763d23d7be3fa1b513545319038795493214
       https://hermes-agent.nousresearch.com/install.sh changed, or the response was rewritten in transit.
       refusing to execute it; nothing was installed
```

A matching fetch prints the hash it checked (`install.sh 68015 bytes, sha256
c73dad4d… matches the pin`) and then runs the script with `--commit
2bd378ed1b8d`. The pin is recorded by the installer itself: `--manifest` prints
`hermes_install_sha256`, and `scripts/manifest.py build` records the same value
at `hermes.install_script_sha256` by asking the installer on the machine for
its `--manifest`. There is no second constant to drift; a manifest built from
a root whose installer pins a different script differs at that line and fails
`manifest.py diff` (and the signed manifest's signature).

**A bump is a deliberate edit**, not a floating dependency:

```sh
$ curl -fsS -o /tmp/install.sh https://hermes-agent.nousresearch.com/install.sh
$ sha256sum /tmp/install.sh
c73dad4dcf7c9ecadb98578e28cc763d23d7be3fa1b513545319038795493214  /tmp/install.sh
$ wc -c /tmp/install.sh
68015 /tmp/install.sh
# edit HERMES_INSTALL_SHA256 (and its "Measured …" comment) in
# installer/odile_install.py, then re-sign in the SAME commit (§7): the
# release key is CI-held, so the release workflow runs this step
$ make release-sign FILE=installer/odile_install.py KEY=<file-key>   # or SIGNER=<ci-signer>
# the self-test checks the shipped signature, so it follows the re-sign:
$ ./installer/odile_install.py --self-test
ok   this installer's shipped signature verifies
ok   the Hermes install-script pin is 64 lowercase hex
ok   a mismatched install script is refused before bash runs
ok   a matching install script reaches bash and installs
self-test: PASS
$ ./installer/odile_install.py --verify-signature --pubkey installer/odile-signing-key.pub
signature: OK (sha256 …)
```

The new installer bytes, the new signature and the rebuilt release manifest
are committed together. `--self-test` includes the shipped-signature check, so
a bump that is not re-signed fails before anything reaches a release.

**The 2026-11-13 bump (ODL-497).** The vendor replaced the script and every
fresh install started refusing it. The new file is 67,104 bytes, `50348fcc…`;
against the 2026-11-09 file (66,864 bytes, `abf33860…`) the whole diff is one
hunk — the pinned uv archive is extracted with `tar -o -xzf`
(`--no-same-owner`) instead of `tar -xzf`, plus a three-line comment saying why
(1 line removed, 4 added; nothing else in the file changes). Release tarballs
record the builder's uid (1001); as root, plain `tar` restores it and hands the
`uv` that root runs to that local account, and `-o` stops that. No new command,
URL, `eval` or privilege — the flag narrows what the extractor does as root.
The constant and its `Measured …` comment moved together and the installer was
re-signed in the same commit: `afdd50b6…` at that head, which also carries
ODL-462's `/verify` page; the cut was `2026.11.13-2` (§12.6). ODL-496 then
re-embedded the tenant bundle on the merged head and re-cut `2026.11.13-3`
(installer `04ca5137…`, 603,247 bytes; §12.6) — the pin itself is unchanged.
The ODL-215 follow-up's backup placement moves the installer and cuts
`2026.11.13-4` (installer `8ab356cf…`, 654,180 bytes; §12.6), which `main` then
re-signed in place as `b8f803c6…` when `75a21aa` removed the live addresses.
ODL-504 carried the website icon onto `/verify` and the first-visit page,
re-embedded both pages on that merged pair and re-cut `2026.11.13-6` (installer
`ef559548…`, 654,258 bytes; §12.6); `-5` belongs to ODL-514, which took and
deployed it. ODL-514 then merged that head and cut `2026.11.13-7` (installer
`6881403b…`, 664,147 bytes; §12.6).

**The 2026-11-14 bump (ODL-497's follow-up).** The vendor replaced the script
a second time and every fresh install started refusing it at the pin. The new
file is 68,015 bytes, `c73dad4d…`; against the 2026-11-13 file (67,104 bytes,
`50348fcc…`) the whole diff is three hunks of git-location hygiene. A new
`drop_git_location_env` unsets an inherited `GIT_DIR`, `GIT_WORK_TREE`,
`GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
`GIT_ALTERNATE_OBJECT_DIRECTORIES` and `GIT_NAMESPACE` before
`stage_repository` and `stage_complete` read the checkout, so a dotfiles alias
or a git hook cannot retarget the rerun's stash/reset at the user's other
repository. The broken-`.git` probe in `stage_repository` now also treats a
`.git` symlink as present and runs `git -C "$INSTALL_DIR"` with
`GIT_CEILING_DIRECTORIES` set to the parent of `INSTALL_DIR`, so a `.git` git
cannot read cannot walk up into a git-managed `$HOME` (#146032). No new
command, URL, `eval` or privilege — the change narrows what git acts on, the
same shape as ODL-497's `tar -o`. The constant and its `Measured …` comment
moved together and the installer was re-signed in the same commit: `fd405391…`
(616,445 bytes). The re-cut from a fresh root whose own run fetched this
script and matched the pin is `2026.11.14-3` (§12.6).

t_9c2d31f1 then dropped the retired demo-reset note from `web/verify.html`
(the demo moved to the tenant at ODL-503 and `docs/CLAIMS.md` v3.8b says there
is no nightly reset), re-embedded the page on that merged installer and cut
`2026.11.14-4` (installer `fc723909…`, 616,368 bytes; §12.6).

The order is proved on a machine with no network: `--self-test` points
`ensure_install` at a local `file://` script and asserts that a mismatched one
is refused with no marker left by `bash`, while a matching one reaches `bash`
and installs. The transcript below was re-measured on 2026-11-14 against the
68,015-byte script the installer now pins; one byte appended:

```sh
$ cp /var/tmp/odile-t_19642a84-vendor-install.sh /var/tmp/odile-t_19642a84-tamper/install.sh
$ printf 'x' >> /var/tmp/odile-t_19642a84-tamper/install.sh
$ sha256sum /var/tmp/odile-t_19642a84-tamper/install.sh
7d615f0b3580bc935ef4e6d459f379ec4359cbc8d2edfe15539516299d8ffcce  /var/tmp/odile-t_19642a84-tamper/install.sh
$ /usr/bin/python3 - <<'PY'
import dataclasses, importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("odile_install", "installer/odile_install.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module          # dataclasses resolve the module by name
spec.loader.exec_module(module)
layout = module.Layout.scratch(root=Path("/var/tmp/odile-19642a84-tamper"), user="odile",
                               webapp_port=9190, meter_port=9191, public_url="http://t.test")
layout.home.mkdir(parents=True, exist_ok=True)
module.ensure_install(dataclasses.replace(
    layout, installer_url=Path("/var/tmp/odile-t_19642a84-tamper/install.sh").as_uri()), False)
PY
install      Hermes as odile into /var/tmp/odile-19642a84-tamper/home/odile/.hermes (pinned 2bd378ed1b8d, browser/cua skipped)
error: the fetched Hermes install script does not match its pinned hash
       fetched  7d615f0b3580bc935ef4e6d459f379ec4359cbc8d2edfe15539516299d8ffcce (68016 bytes)
       pinned   c73dad4dcf7c9ecadb98578e28cc763d23d7be3fa1b513545319038795493214
       file:///var/tmp/odile-t_19642a84-tamper/install.sh changed, or the response was rewritten in transit.
       refusing to execute it; nothing was installed
$ echo $?
1
```

`/var/tmp/odile-19642a84-tamper` holds only the empty `home/odile` the snippet
made: no temp script, no launcher, and no `bash` process. The user named in the
layout need not exist — the refusal is that early. The shape is ODL-399's
2026-11-09 one; only the fetched hash, byte count and pin move.

## 5. Re-runs

```sh
$ ./odile_install.py --recovery-kit recovery-kit.json
same         /usr/local/bin/odile-install.sh
boot         release 2026.11.1 · /run/blackbox/attest.json
             measurement 519c7c9a…9ee8
             boot key    SHA256:zaZC…kC0
same         boot record /etc/odile/boot-record.json (release 2026.11.1)
same         seed cache /etc/odile/seed.json (signed, instance …)
same         /etc/odile/key.env
…
same         /home/odile/.hermes/config.yaml (5 managed blocks, sha256 …)
same         /home/odile/.hermes/skins/odile.yaml (skin odile)
same         /home/odile/.hermes/config.yaml (display.skin odile)
same         Webapp brand (odile-brand.js v…, index.html linked)
same         /home/odile/.hermes/.env (mode 0600, 3 allowlisted keys, allowlist ok)
…
tenant: same
```

A re-run without `--expect` reads `/etc/odile/boot-record.json`; a moved
measurement stops with exit 5 instead of reusing the old number. After a
release move, run the verifier again and pass its new number (§3a).

`--dry-run` is safe on a machine with nothing on it (it needs no user, no key
and no network) and is the first command to run when something looks wrong.
`--force-smoke` re-runs the paid chat turn; without it a converged machine does
not spend money.

## 6. The machine-free proof

Three commands, no tenant machine:

```sh
/usr/bin/python3 installer/odile_install.py --self-test
/usr/bin/python3 installer/odile_install.py --dry-run --recovery-kit <kit>
/usr/bin/python3 installer/sim_tenant.py              # first time
/usr/bin/python3 installer/sim_tenant.py --reset --apply   # start over (prints what it deletes first)
```

The harness is a fresh-run proof: it issues one setup token, and at the end it
stops the scratch services and reports. A second pass therefore starts with
`--reset --apply`; a plain re-run against a converged scratch root reuses the
installer's cached seed (so the freshly issued token is never consumed) and its
replay assertion is not meaningful.

The simulation creates an unprivileged user (`odiletest`) and a scratch root
under `/var/tmp/odile-sim`, runs a scratch seed service with a real NEAR key
read at run time from `/root/.hermes/.env`, and runs the installer **as that
user** with `--root` twice. It then asserts, independently of the installer's
own smoke output:

- run 1 installs (`tenant: provisioned`), run 2 prints `tenant: same` and no
  line beginning `create|write|build|install|start|restart|rewrite|chmod|enable`;
- `<root>/usr/local/bin/odile-install.sh` is byte-identical to the running
  `installer/odile_install.py`, mode 0755, and run 2 reads `same` for it — the
  path `--manifest` and CLAIMS §4.6 hash (ODL-332 follow-up);
- run 1 fetched `install.sh` over HTTPS, and its log names the size and hash it
  checked before `bash` (`install.sh 68015 bytes, sha256 c73dad4d… matches the
  pin`, §4b);
- the sign-in gate is 302/401/200, probed over HTTP by the harness through
  the installer's `login_table` (`deploy/demo_site.gate_ok`'s shape); the
  review's own curl probe on the scratch port is the independent cross-check;
- the chat turn's shell command and file write happened, and the cron job ran;
- the per-user key is at `/etc/odile/key.env` (0600), the gateway's process
  environment holds it, and the profile `.env` carries no inference key;
- the setup token is refused with `410 used` on replay, and a backdated token
  with `410 expired`;
- the recovery file carries the backup password, the fingerprint placeholder,
  and browser-only instructions that name the agent (no `scp`, `shred` or
  `root@`).

The boot check is on the same code path: a scratch run with no report prints
`skip boot check (simulation)` and continues (so the harness above still runs);
a run with `--boot-report <fixture> --expect <96-hex>` asserts the report and
writes `/etc/odile/boot-record.json`, and a second run reads `same boot record`.
A mismatching `--expect` exits 5 before the seed cache or exchange.

Measured 2026-11-08 on HQ (this box), `simulation: PASS`:

```
$ /usr/bin/python3 installer/sim_tenant.py --reset --apply
reset        root /var/tmp/odile-sim and user odiletest
deleted      /var/tmp/odile-sim
deleted      user odiletest
create       user odiletest (home /var/tmp/odile-sim/home/odiletest, no sudo)
seed         /root/.odile/installer-sim/seed.db · tenant odl-sim-203 · instance srv-odl-sim-203
listening    seed 127.0.0.1:8082
run          1: exit 0 in 4m 13s; log /var/tmp/odile-sim/evidence/run1.log
ok   the first run exits 0
ok   the first run provisioned
ok   GET / without a session -> 302 to /login: 302
ok   GET /api/sessions without a session -> 401: 401
ok   POST /auth/password-login, the Odile password -> 200: 200
ok   GET / with the session cookie -> 200 Webapp shell: 200 shell=True
ok   the gate is 302/401/200 (deploy_demo_site.gate_ok shape): 302/401/200
ok   one shell command + one file write: …/workspace/smoke.txt = ['odile-smoke-ok', 'odiletest']
ok   one cron job ran: …/workspace/cron-proof.log = 2026-11-08T12:27:53Z odile-cron-ok
ok   the chat went through the meter: 3 meter row(s) in /var/tmp/odile-sim/var/lib/odile/meter.db
ok   the recovery file carries the backup password
ok   the recovery file carries the fingerprint placeholder
ok   the recovery file prints the copy-off and shred commands
meter        (requests, prompt, completion, usd) = (3, 7121, 85, 0.00016854)
ok   a replayed token is refused: 410 {'error': 'token already used', 'reason': 'used'}
ok   an expired token is refused: 410 {'error': 'token expired', 'reason': 'expired'}
run          2: exit 0 in 0m 1s; log /var/tmp/odile-sim/evidence/run2.log
ok   the second run exits 0
ok   the second run prints `tenant: same`
ok   the second run changes nothing: 0 write line(s)
simulation: PASS (21 passed, 0 failed)
```

ODL-471 re-cut the recovery-file copy, so those three recovery lines now read
`ok the recovery file carries the backup password`,
`ok the recovery file carries the fingerprint placeholder`,
`ok the recovery file is browser-only (no scp, shred or root@)` and
`ok the recovery file names the agent as the way to see it again` (22 checks).
The block above is the 2026-11-08 run before that change; `--self-test` proves
the new copy, and the full simulation is re-run by ODL-332 on a tenant.

Run 1's 4m13s here was a warm Hermes runtime; a colder run took 14m47s when
the runtime had to be downloaded. The installer's own steps are the demo host's
plus the tenant legs; neither the simulation nor its timing is a claim about a
tenant VM's wall time (ODL-332).

### The installer's own copy (ODL-332 follow-up)

Measured 2026-11-11 on HQ, the same harness, against installer sha256
`ad04d570…`:

```
$ mkdir -p /root/.odile/installer-sim /var/tmp/odile-472-sim-merged-ledger
$ unshare -m bash -c 'mount --bind /var/tmp/odile-472-sim-merged-ledger /root/.odile/installer-sim && exec /usr/bin/python3 installer/sim_tenant.py --root /var/tmp/odile-472-sim-merged --user odile472m --port 9340 --meter-port 9341 --seed-port 9342 --reset --apply'
…
run          1: exit 0 in 3m 19s; log /var/tmp/odile-472-sim-merged/evidence/run1.log
ok   the first run exits 0
ok   the first run provisioned
…
run          2: exit 0 in 0m 1s; log /var/tmp/odile-472-sim-merged/evidence/run2.log
ok   the second run exits 0
ok   the second run prints `tenant: same`
ok   the second run changes nothing: 0 write line(s)
simulation: PASS (22 passed, 0 failed)
```

The private mount is the harness note `update.md` §5 carries: the seed
ledger path is hardcoded and a concurrent card's `--reset` deletes it mid-run.

Run 1's log places the copy before the boot step; run 2 reads `same` for it:

```
$ sed -n '4,6p' /var/tmp/odile-472-sim-merged/evidence/run1.log
signature    OK (sha256 ad04d570217f1c7e59870b6d533403fab1ee24335e8eca04c27f32952742b05b)
write        /var/tmp/odile-472-sim-merged/usr/local/bin/odile-install.sh (this program, sha256 ad04d570217f…)
skip         boot check (simulation)
$ sed -n '5p' /var/tmp/odile-472-sim-merged/evidence/run2.log
same         /var/tmp/odile-472-sim-merged/usr/local/bin/odile-install.sh
```

A pass with a boot report writes the record after the copy, and the record
carries that same hash — measured with the previous installer this section
replaced (`04b3993b…`):

```
$ … --boot-report /var/tmp/odile-sim/attest-fixture.json --expect 519c7c9a…9ee8
same         /var/tmp/odile-sim/usr/local/bin/odile-install.sh
boot         release 2026.11.1 · /var/tmp/odile-sim/attest-fixture.json
write        /var/tmp/odile-sim/etc/odile/boot-record.json (boot record, release 2026.11.1, 6 vCPU, installer 04b3993b211d…)
```

A second pass over the same record prints `same boot record … (release
2026.11.1)`, so the boot half is idempotent too.

The two values CLAIMS §4.6 compares, and the manifest's own row, agree:

```
$ sha256sum /var/tmp/odile-472-sim-merged/usr/local/bin/odile-install.sh installer/odile_install.py
ad04d570217f1c7e59870b6d533403fab1ee24335e8eca04c27f32952742b05b  /var/tmp/odile-472-sim-merged/usr/local/bin/odile-install.sh
ad04d570217f1c7e59870b6d533403fab1ee24335e8eca04c27f32952742b05b  installer/odile_install.py
$ /usr/bin/python3 scripts/manifest.py build --root /var/tmp/odile-472-sim-merged --user odile472m \
    --generated-at 2026-11-11T16:45:00Z --out /tmp/sim-manifest.json
manifest     /tmp/sim-manifest.json (sha256 568a0a40d79350ffc2eec898509b8baafd397c41107df1b486396479e2ef63cd, 48 skills, 9 packages, audit null)
$ /usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["installer_sha256"])' /tmp/sim-manifest.json
ad04d570217f1c7e59870b6d533403fab1ee24335e8eca04c27f32952742b05b
```

The scratch root is a simulation, so the copy there is owned by `odile472m`;
`Layout.tenant` maps the same absolute path and uid/gid 0, which is what a
tenant install writes. The manifest's own sha256 moves with its `generated_at`;
its `installer_sha256` row does not. `audit_sha256` stays null: ODL-208's
`scripts/audit.py` does not exist yet (a separate card), so this change does
not invent an audit row.

### The first run is Odile's, not Hermes's

The installer writes the standing context and the embedded skills before the
gateway starts, and the config carries `onboarding.run: false` and
`onboarding.profile_build: "off"`. Measured on a fresh scratch root
(`/var/tmp/odile-sim-387`, 2026-11-08), run 1 printed:

```
write        …/home/odiletest387/AGENTS.md (templates/tenant-agents.md, sha256 78710f841071…)
create       …/home/odiletest387/.hermes/skills/odile-first-run (mode 0o700)
write        …/home/odiletest387/.hermes/skills/odile-first-run/SKILL.md (skills/odile-first-run/SKILL.md, sha256 0ca5d154b315…)
write        …/home/odiletest387/.hermes/.odile-managed-skills.json (the embedded skill names)
```

and run 2 printed `same` for each, then `tenant: same`. A bundle reduced by
one skill removed that skill on the next run (`remove … (no longer in the
bundle)`); the restored bundle put it back. A skill the user added is not in
the manifest and is left alone. The one-byte pin proof is in §7.

The Webapp's first load showed `Sessions / No sessions yet` and opened no
onboarding chat; `state.db` held no `onboarding_run` key in any session. The
first message (`Hi`) loaded `odile-first-run` and answered with the greeting
and question 1, not a Hermes introduction:

> I'm Odile, the agent on this machine. Your own computer, with me inside it. No
> one at Odile can log in unless you grant it. I run as `odile` with sudo, so I
> can read every file on the machine. Built on Hermes. Five short questions,
> then I'll get to work. What should I call you?

To reproduce the browser leg, map the simulation's public host to loopback
(`127.0.0.1 odile-sim.test` in `/etc/hosts`) and open
`http://odile-sim.test:9150/`, signing in with the simulation password. The
same check without a browser is a `Host: odile-sim.test:9150` header plus the
session cookie from `/auth/password-login`; then read the state:

```
$ cd /var/tmp/odile-sim-387/home/odiletest387/.hermes
$ /usr/bin/python3 - <<'EOF'
import sqlite3
rows = list(sqlite3.connect("state.db").execute("select id, model_config from sessions"))
print(len(rows), sum(1 for _, cfg in rows if "onboarding_run" in (cfg or "")))
EOF
```


### 6.1 The OS package pins

`ensure_packages` installs every package as `name=version` from one dated
Debian snapshot; there is no bare `apt-get install` left in the installer.

| What | Value |
|---|---|
| suite | Debian 13 `trixie` (13.7, published 2026-09-12) |
| snapshot | `20261108T144109Z` — <https://snapshot.debian.org/archive/debian/20261108T144109Z> |
| required | `caddy=2.6.2-12+deb13u1` `curl=8.14.1-2+deb13u5` `git=1:2.47.3-0+deb13u1` `nodejs=20.19.2+dfsg-1+deb13u2` `npm=9.2.0~ds1-3` `openssl=3.5.7-1~deb13u2` `python3=3.13.5-1` `restic=0.18.0-1+b4` `tmux=3.5a-3` |
| best effort | `chromium=150.0.7871.181-1~deb13u1` (a failure skips it; never "latest") |

The step writes `/etc/odile/apt-pins.list` and runs apt with
`-o Dir::Etc::sourcelist=<that file> -o Dir::Etc::sourceparts=- -o
Dir::State::lists=/var/cache/odile-apt/lists`. The tenant's own
`/etc/apt/sources.list` is never edited, and the whole transaction — every
dependency too — resolves inside the snapshot. A package already at its pin
reads `same`; a package at another version is converged with
`--allow-downgrades`, because a tenant's manifest must record the pins, not the
image's drift.

The installer's own record of the table (the pins and snapshot are unchanged
since the reviewed ODL-214 head; `installer_sha256` and `backup_sha256` move
with the files they name — `sha256sum` prints the current files' hashes, and
`--manifest` agrees with them):

```sh
$ ./installer/odile_install.py --manifest
  "installer_sha256": "fc723909051a0d589bc6738b0901561b5eef1f47116100830ac8b9954814120b",
  "backup_sha256": "d9c4278a0660650e7edc6b50847d96cdbb71ca7709c52402257e94a291a98955",
  "os_snapshot": "20261108T144109Z",
  "os_snapshot_url": "https://snapshot.debian.org/archive/debian/20261108T144109Z",
  "os_suite": "trixie",
  "os_packages": {
    "caddy": "2.6.2-12+deb13u1",
    "curl": "8.14.1-2+deb13u5",
    "git": "1:2.47.3-0+deb13u1",
    "nodejs": "20.19.2+dfsg-1+deb13u2",
    "npm": "9.2.0~ds1-3",
    "openssl": "3.5.7-1~deb13u2",
    "python3": "3.13.5-1",
    "restic": "0.18.0-1+b4",
    "tmux": "3.5a-3"
  },
  "os_optional_packages": {
    "chromium": "150.0.7871.181-1~deb13u1"
  },
```

The proof, on a bare Debian 13 root — no tenant, no kit, no key:

```sh
$ debootstrap --variant=minbase --arch=amd64 \
      --include=ca-certificates,python3-minimal trixie \
      /var/tmp/odl214pin-root http://deb.debian.org/debian
…
# the image the installer runs on has the full python3; a minbase chroot needs
# it for the harness (the installer itself imports getpass):
$ chroot /var/tmp/odl214pin-root apt-get update -qq
$ chroot /var/tmp/odl214pin-root apt-get install -y -qq --no-install-recommends python3
$ cp installer/odile_install.py /var/tmp/odl214pin-root/tmp/
$ cat > /var/tmp/odl214pin-root/tmp/pin_harness.py <<'PY'
"""Drive odile_install.ensure_packages on a bare Debian 13 chroot."""
import importlib.util
import subprocess
import sys

spec = importlib.util.spec_from_file_location("odile_install", "/tmp/odile_install.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["odile_install"] = mod
spec.loader.exec_module(mod)
layout = mod.Layout.tenant(user="odile", webapp_port=9120, meter_port=8010,
                           public_url="https://odl-test.odile.fyi")

mode = sys.argv[1] if len(sys.argv) > 1 else "install"
if mode == "badpin":
    mod.OS_PACKAGE_PINS["tmux"] = "0.0.0-not-a-version"
    mod.ensure_packages(layout, dry=False)
    print("UNREACHABLE: a bad pin did not stop the install")
    sys.exit(9)

changed = mod.ensure_packages(layout, dry=False)
print("ensure_packages changed:", changed)
ok = True
for name, pin in mod.OS_PACKAGE_PINS.items():
    out = subprocess.run(["dpkg-query", "-W", "-f=${Version}", name],
                         capture_output=True, text=True).stdout.strip()
    same = out == pin
    ok = ok and same
    print(f"{'ok  ' if same else 'MISMATCH'} {name} {out} (pin {pin})")
print("packages.os == pins:", ok)
sys.exit(0 if ok else 1)
PY
$ chroot /var/tmp/odl214pin-root /usr/bin/python3 /tmp/pin_harness.py install
install      OS packages (9 pinned to Debian 13 trixie, snapshot 20261108T144109Z)
             caddy=2.6.2-12+deb13u1 curl=8.14.1-2+deb13u5 git=1:2.47.3-0+deb13u1
             nodejs=20.19.2+dfsg-1+deb13u2 npm=9.2.0~ds1-3 restic=0.18.0-1+b4
             tmux=3.5a-3
             chromium=150.0.7871.181-1~deb13u1 (best effort)
write        /etc/odile/apt-pins.list (snapshot 20261108T144109Z)
install      chromium=150.0.7871.181-1~deb13u1 (best effort)
ensure_packages changed: True
ok   caddy 2.6.2-12+deb13u1 (pin 2.6.2-12+deb13u1)
ok   curl 8.14.1-2+deb13u5 (pin 8.14.1-2+deb13u5)
ok   git 1:2.47.3-0+deb13u1 (pin 1:2.47.3-0+deb13u1)
ok   nodejs 20.19.2+dfsg-1+deb13u2 (pin 20.19.2+dfsg-1+deb13u2)
ok   npm 9.2.0~ds1-3 (pin 9.2.0~ds1-3)
ok   openssl 3.5.7-1~deb13u2 (pin 3.5.7-1~deb13u2)
ok   python3 3.13.5-1 (pin 3.13.5-1)
ok   restic 0.18.0-1+b4 (pin 0.18.0-1+b4)
ok   tmux 3.5a-3 (pin 3.5a-3)
packages.os == pins: True
```

Two of the nine were already at the pin in the chroot (`openssl` came in with
`ca-certificates`, `python3` with the harness prep), so the run names seven
`name=version` arguments and then verifies all nine. A second run reads
`same OS packages (9 pinned to Debian 13 trixie, snapshot 20261108T144109Z)` and
`ensure_packages changed: False`. One pin that does not exist stops the install
before apt changes anything:

```sh
$ chroot /var/tmp/odl214pin-root /usr/bin/python3 /tmp/pin_harness.py badpin
install      OS packages (9 pinned to Debian 13 trixie, snapshot 20261108T144109Z)
             tmux=0.0.0-not-a-version
             converging tmux 3.5a-3 -> 0.0.0-not-a-version
same         /etc/odile/apt-pins.list
error: the pinned OS packages are not in the snapshot 20261108T144109Z:
       tmux=0.0.0-not-a-version
       E: Version '0.0.0-not-a-version' for 'tmux' was not found
       refusing to fall back to the latest version
$ echo $?
1
```

**Upgrade path.** A new pin is a new installer release, never an `apt upgrade`
on the tenant: edit `OS_PACKAGE_PINS` and `OS_SNAPSHOT` together (every version
must exist in that snapshot), run the chroot proof above, re-sign (§7) and
release. The release record is `--manifest` (`os_snapshot`, `os_packages`) plus
`installer_sha256`; a tenant's `./scripts/manifest.py build` must show the same
`installer_sha256` and a `packages.os` equal to `os_packages`. Changing a
version without the snapshot — or the snapshot without the versions — fails the
proof above.

## 7. The signature

Verify (this is also step 2 of the ritual):

```sh
$ ./installer/odile_install.py --verify-signature --pubkey installer/odile-signing-key.pub
signing key  sha256 13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2 · SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8
signature: OK (sha256 fc723909051a0d589bc6738b0901561b5eef1f47116100830ac8b9954814120b)
```

The first value is `sha256sum` of the exact PEM file the run used — here the
shipped `installer/odile-signing-key.pub`; the second is the same key's
`SHA256:` fingerprint (SHA-256 over the SPKI DER, the spelling
`scripts/manifest.py` and `odile-update` print). Compare the digest with the
published file:

```sh
$ curl -s https://odile.fyi/odile-signing-key.pub | sha256sum
13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2  -
```

A recovery kit that carries `signing_key_sha256` must name that digest, or the
installer exits 3 before the seed exchange and writes nothing.

A one-byte edit anywhere — including a change made after a valid signature was
issued — fails against the same detached signature:

```sh
$ cp installer/odile_install.py /tmp/edited.py
$ printf 'x' >> /tmp/edited.py
$ sha256sum /tmp/edited.py
95e66fa9b5adc35fef58b20357843b1629cc629d6079bb784a031538dd6b58ae  /tmp/edited.py
$ /usr/bin/python3 /tmp/edited.py --verify-signature \
      --pubkey installer/odile-signing-key.pub --signature installer/odile_install.py.sig
signing key  sha256 13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2 · SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8
signature: BAD (sha256 95e66fa9b5adc35fef58b20357843b1629cc629d6079bb784a031538dd6b58ae)
$ echo $?
3
```

Recorded on 2026-11-14 from the merged installer that carries the handoff audit
(ODL-208's `/usr/local/bin/odile-audit`, re-cut by the follow-up that reads
`authorized_keys2` and scans `/root` for the boot key's private half) beside
the strict-bar help surface (ODL-470's five answers), ODL-457's first-visit
service and page, ODL-471's recovery-file download, ODL-472's `/manifest`
routes and first-boot placement, ODL-473's `/verify` report, the tenant
template's `reasoning_effort` at `6dfb682`, the ODL-471 follow-up's refused
page, the shell scale on the two embedded pages, ODL-462's v3.9 claims
landing (the re-cut `/verify` gap 2 and gap 3 blocks), t_e237e261's meter-free
installer (no embedded meter, unit or user; the seed's per-user NEAR key is
written to `/etc/odile/key.env`), ODL-497's Hermes
install-script re-pin (now `c73dad4d…` after the 2026-11-14 follow-up, §4b),
ODL-496's re-cut of the tenant's
standing rules (`$HOME/AGENTS.md`, the A5 reboot answer), the ODL-215
follow-up's backup placement (`scripts/backup.py` at
`/usr/local/lib/odile/backup.py` plus the two `odile-backup-tenant` units),
and ODL-504's re-embed of `/verify` and the first-visit page with the website
icon links on the merged head that also carries `main`'s re-signed `-4` pair
(`75a21aa`) and ODL-514's seed-certificate chain (the release key certifies the
seed key; the first boot and the installer both walk it), sha256 `fc723909…`
(616,368 bytes, `--verify-signature` OK); the edited copy above is 616,369
bytes. The BAD line
prints the edited copy's own sha256 — the `sha256sum` above — so the hex moves
whenever the installer's bytes do; `signature: BAD` and exit 3 are the
invariant. The edited copy is a different `INSTALLER_PATH`, so this runs the
*whole* installer and it still checks its own bytes. A tenant run does the same
check before the first state change; a bad signature exits 3 with nothing
written.

Re-signing runs in the **release workflow** (ODL-517): the release key is
CI-held and never lives on HQ, so the release job invokes the same target the
Makefile uses. `KEY=` is the file form (a hand-run or a test; a `KEY=` that
resolves under `/root` is refused); `SIGNER=` is the CI form ODL-515 adds for
the backend on `t_54b8f354` (Security recommends Cloud KMS Ed25519 via OIDC,
key non-exportable). `make release-sign` prints the file's `sha256sum` before
it writes the signature, and verifies the result against
`installer/odile-signing-key.pub` (override with `PUBKEY=`):

```sh
$ ./installer/embed_tenant.py       # only when the tenant bundle changed
$ make release-sign FILE=installer/odile_install.py KEY=<file-key>   # or SIGNER=<ci-signer>
<sha256 of the installer>  installer/odile_install.py
<base64 signature>
write        installer/odile_install.py.sig (base64, 64 bytes)
verify       OK against installer/odile-signing-key.pub (sha256 …)
$ ./installer/odile_install.py --verify-signature --pubkey installer/odile-signing-key.pub
```

The same job cuts the seed certificate (`make seed-cert`) and the measurement
pin (`make pin-sign`, ODL-515); the order and the key move are in
[`seed.md`](seed.md) §3a, and ODL-517 runs it.

The embedded tenant files are pinned by SHA-256 inside the installer; `--self-test`
checks each pin and, in a checkout, that the embedded copy equals its source.
`--manifest` lists every target, source and hash. `--self-test` also verifies
this file against the `.sig` beside it, so an edit that is not re-signed —
including a bump of `HERMES_INSTALL_SHA256` (§4b) — fails the self-test in the
same working tree, before a release is built.

`make check` runs `--self-test` as its `self-test` step, so the gate now
requires the re-embed and re-sign in the same commit: an edit to the installer or
the tenant bundle that leaves the shipped pair stale fails the run with the
self-test's own `FAIL` line. `make prereqs` names `openssl`,
which the signature check shells out to.

## 8. The seed exchange

The installer is a client of ODL-204; the contract, the lone ledger and the
token rotation live in [`seed.md`](seed.md). Before the exchange the kit's
`signing_key_sha256` must match the digest the installer printed for the key it
used (§3, §7). The installer refuses a seed that is missing a field, carries a
password/passphrase/private-key field, names a different instance or slug, or
whose signature does not verify — before it parses any field or writes any file.

Two keys sign, and the seed body walks one chain. The kit's
`signing_public_key` is the **release** key: it signs this installer. The seed
body is signed by the **seed** key, and carries that key's public half
(`signing_key`), the SHA-256 of those bytes (`signing_key_sha256`) and the
release key's signature over them (`signing_key_cert`). The installer verifies
the certificate against the release key — in the kit, beside this installer, or
in `installer/odile-signing-key.prev.pub` for the release that ships the key
being retired — then verifies the body against the certified seed key, and
prints which release key certified it:

```
seed key     sha256 <64 hex> certified by …/odile-signing-key.pub
```

A seed with no `signing_key_sha256` was issued before ODL-514: it is verified
against the release key directly, so an installed machine keeps working. A
certificate signed by a third key is refused with the reason on one line, and
the installer writes nothing.

## 9. What the simulation cannot prove (ODL-332)

- the measured command line that carries the boot key (a throwaway public key
  under the strict bar, not a user key);
- the real `/run/blackbox/attest.json` that Servury's `bb-seed` writes at
  switch-root: ODL-396 exercises the reader, the decision and the record
  against a byte-identical copy of HQ's report and fixtures; the first
  tenant-machine boot transcript is ODL-332;
- the sealed hook opening the pre-sealed disk with no passphrase at all;
- DNS for a real `<slug>.odile.fyi` and Cloudflare's answer;
- ACME against Let's Encrypt and the issued certificate's fingerprint;
- the real `systemd`, `sshd`, `visudo` and Caddy restart paths (the simulation
  writes the same files and skips the daemons);
- the tenant VM's own resources (vCPU, RAM) and the Webapp build's wall time on
  it.

The boot-related legs are observed on the real machine (2026-11-09 and again
on 2026-11-11): the measured command line with exactly one `bb.key`, the
initramfs host key that survives a restart, and the no-operator-login probe.
The custom measured UKI removes the encrypt/unlock/install steps that used to
wait on the owner's boot key; DNS/ACME and the issued certificate wait on the
panel's image step. §12 has the measured state and the exact next commands.

Everything else — the seed, the signature, the scrypt hash, the config merge,
the Hermes install and Webapp build, the sign-in gate, the smoke —
runs on the installer's real code path in the simulation.

## 10. Notes and known gates

- **Port open is not readiness.** uvicorn binds `127.0.0.1:9120` seconds before
  the dashboard answers and returns `503` in between; the installer waits for
  the first non-5xx from `/`, not for the socket.
- **The Webapp build is the memory peak.** `vite` transforms ~16k modules and
  peaks near 2.4 GB. On a small or busy host V8's default heap gets node
  OOM-killed (measured on HQ); the installer caps the old-space heap at 60% of
  RAM, clamped to [1536, 3072] MB, via `NODE_OPTIONS` for the build only.
- **DNS/ACME** each get 15 minutes of retries; a failure there exits non-zero
  after everything else has converged, and a re-run resumes.
- **Smoke costs money.** One chat turn against NEAR AI, about a tenth of a
  cent at the pinned model's prices; `--force-smoke` spends it again.
- **The package step is snapshot-bound.** Every package comes from
  `snapshot.debian.org`, which is slower than the CDN: the first install pays
  a minute or two for the index and the debs. If the snapshot or a pin is
  unreachable the install stops with `refusing to fall back to the latest
  version` idempotently — nothing else in the install depends on it, and a
  re-run resumes.

## 11. Audit

```sh
./installer/odile_install.py --manifest        # hashes (incl. the Hermes install-script pin), pinned commit, OS package pins + snapshot, managed keys, allowlist, tenant files
./installer/odile_install.py --print-source     # the whole program, including the tenant bundle
sha256sum installer/odile_install.py           # matches --manifest
sha256sum /usr/local/bin/odile-install.sh      # on a tenant: == --manifest installer_sha256
```

The program is standard-library Python 3.13+; its only external commands are
the ones the install needs (`apt-get`, `useradd`, `systemctl`, `visudo`, `sshd`,
`caddy`, `openssl`, `tmux`, `sudo`).

## 12. The first tenant — the strict run (ODL-332)

The ceremony is deleted (Product Lead ruling 2026-11-10). A tenant does not see
a terminal, a passphrase or a boot key: the machine boots a custom measured
UKI, the disk it opens is the pre-sealed image the panel wrote, the first page
is the sign-in, and `/verify` is the machine's own access report. This section
is the live run; the measured state and the transcripts are in
[`docs/evidence/m1-tenant.md`](../evidence/m1-tenant.md).

The one human action is the panel step (§12.3). Everything before it is a
builder command with a transcript; everything after it is the image.

### 12.1 Preconditions measured on 2026-11-11

| # | Precondition | State on 2026-11-11 |
|---|---|---|
| 1 | the machine is the test tenant, active to 2026-12-10 | yes — `31a63bbb` / `odl-7971a8` / `203.0.113.87`, plan 113, SEV-SNP, measured boot, FDE requested |
| 2 | a live seed service behind `POST https://odile.fyi/seed` | yes — unit `odile-seed` active; `GET /seed` → 405 |
| 3 | an A record `<slug>.odile.fyi` | yes — `odl-7971a8.odile.fyi → 203.0.113.87`, DNS-only |
| 4 | the spine is at `first_boot`, no ceremony state | yes — `tenant_state.first_boot`; `waiting_for_ceremony`, `installing` and `active` are retired (ODL-460) |
| 5 | the signed measured UKI and the pre-sealed disk | **yes, round 8 at 10:28Z, saved 14:42:53Z and booted 14:43:54Z** — UKI `5cb31808…`, disk `c1278242…`, measurement `9f01725a…`, 4-vCPU pin, published 10:29Z; the earlier builds and the defect each exposed are in `docs/evidence/m1-tenant.md` §9–§9.8 and `docs/evidence/m1-image.md` |
| 6 | a fresh setup token bound to `31a63bbb` | **yes** — ledger `67de1ba9…`, live until 2026-11-13T10:23Z; its plaintext is the `odile.token=` of the sealed line |
| 7 | the owner's boot key, a passphrase, any operator key | **none** — the image's `bb.key` is a throwaway public key whose private half is shredded |
| 8 | a signed installer a stranger can obtain (independent of the image) | **yes** — on 2026-11-11 the served release `2026.11.11-5` shipped the strict-bar installer `fde7a48f…`, its `.sig`, the published key and a skills tarball carrying `odile-recipes` (§12.6); the repository's cut is `2026.11.14-4` (`fc723909…` installer, `fcf0bc61…` manifest, §12.6; the served URL serves that same `2026.11.14-4` — measured at deploy 2026-11-14T09:48Z and re-measured 2026-11-14T11:42Z: installer `fc723909…`, 616,368 bytes, manifest `fcf0bc61…` — and that live cut carries the `c73dad4d…` vendor pin), and an image a build of this tree makes carries that same pair in its `release/` directory, so the machine's `/manifest` and `/usr/local/bin/odile-install.sh` agree with each other. The live tenant's image is still `2026.11.11-8` (`4aee80c5…`, `1ed9dcad…`) until it is re-provisioned |

### 12.2 The builder's half (everything before the panel)

```sh
# 1. a fresh token, bound to the Servury server_id the image's line will carry
/usr/bin/python3 /opt/odile/seed/seed.py token rotate --tenant odl-7971a8 --instance 31a63bbb

# 1b. the per-tenant measured line. It must carry every field Servury's own
#     release line carries: the release installer's boot check requires exactly
#     one bb.ip4= and exits 5 without it (measured, round 4), and the disk is
#     sealed to these exact bytes. Values: the stock serial's bb.* fields for
#     this machine, a throwaway public key, a fresh random seal id, the token
#     from step 1. The panel's command line stays empty (the UKI carries this).
cat > "$OUT/boot/cmdline" <<'EOF'
root=/dev/mapper/root_crypt ro console=tty0 console=ttyS0,115200 consoleblank=0 systemd.show_status=1 ip=203.0.113.87::203.0.113.1:255.255.255.0:odl-7971a8:ens18:off bb.release=2026.11.2 bb.host=odl-7971a8 bb.iid=31a63bbb bb.ip4=203.0.113.87/24 bb.gw4=203.0.113.1 bb.ip6=2001:0db8:0:263::1/64 bb.gw6=2001:0db8::1 bb.dns=1.1.1.1,9.9.9.9,2606:4700:4700::1111,2620:fe::fe bb.key=<base64 of the throwaway public key> seal_id=<16 hex> odile.token=<token from step 1> odile.seed_url=https://odile.fyi/seed
EOF
# `sealed-image.sh --cmdline-file` refuses a line without exactly one
# bb.ip4=, so the defect the tenant found cannot ship again.

# 2. the initramfs with Servury's real snpreport and the hook
./deploy/image/sealed-initrd.sh --out "$OUT" --snpreport "$OUT/snpreport"

# 3. the disk, from the merged recipe, sealed to the per-tenant line
./deploy/image/sealed-image.sh --out "$OUT" --production --rootfs "$OUT/rootfs" \
    --cmdline-file "$OUT/boot/cmdline" --force

# 4. the signed UKI and the pin
./deploy/image/uki.sh --in "$OUT" --sign --pin "$OUT/tenant-image.pin"

# 5. the pin inside the disk: /verify must check the measurement the user was
#    given, and odile-release-setup refuses to run without it
./deploy/image/place-pin.sh --disk "$OUT/odile-tenant-sealed.raw" \
    --pin "$OUT/tenant-image.pin"

# 6. publish both at an http(s) URL with their SHA-256 (the panel's two fields)
install -d -m 0755 /srv/odile-releases/m1
install -m 0644 "$OUT/odile-tenant-sealed.raw" /srv/odile-releases/m1/
install -m 0755 "$OUT/odile-tenant.efi" /srv/odile-releases/m1/
```

`OUT` is outside the repo (the image is a build artefact). `--production`
writes the bootstrap key `/odile/luks-bootstrap.key` on the boot partition,
drops the QEMU network unit, installs `odile-seal-provision.service` and
**takes no bench stand-in**: no guest smoke, no bench proxy sockets and no
`odile-bench-attest.service`, which would overwrite Servury's real
`/run/blackbox/attest.json` with a value derived from the command line. The
published disk was inspected read-only: the pin is at
`/etc/odile/tenant-image.pin` and the enabled Odile units are exactly the six
production ones. `docs/evidence/m1-image.md` §3 records the two defects the
first production build exposed and the fix.

`uki.sh` writes a new random `seal_id` and a new boot key per build, and
`sbsign` is not deterministic, so **every rebuild changes the measurement and
the two hashes together**. Publish the pair from the same build that produced
the pin; never mix a hash from one build with a file from another.

The line in `boot/cmdline` carries `bb.iid=31a63bbb`, `bb.host=odl-7971a8`,
the network parameters of the provider record, a throwaway `bb.key`, a random
`seal_id`, the fresh `odile.token` and `odile.seed_url=https://odile.fyi/seed`.
It is the only per-tenant input of the sealed design. Never reuse the stock
guest UUID `01494715…` for `bb.iid`: the seed token is bound to the API
`server_id` (`31a63bbb`), and a first-boot POST with any other id is refused
`403 wrong_instance` (measured in ODL-456).

### 12.3 The panel step (the one human action)

Manage → **Boot image** and Manage → **Disk image**. Both take an http(s) URL
and a SHA-256; the node checks the hashes, stops the machine, snapshots and
discards the old disk, writes the image with `qemu-img convert`, and starts the
machine. The command-line field stays empty when the per-tenant line is inside
the UKI's `.cmdline` section (Servury's measured-boot guide §7). §4 of
[`docs/evidence/m1-tenant.md`](../evidence/m1-tenant.md) carries every round's
pair and names the current one (round 8, card `t_d41b2384`, saved 14:42:53Z;
§9.8 is the boot it produced).

A reinstall always comes back on the Servury release, because it replaces the
disk. That is the rollback: **Back to the Servury release** plus a reinstall
returns a stock machine, and the strict run starts again from §12.2.

### 12.4 What the first boot does

1. the initramfs hook `bb-sealed-unlock` reads the sealed blob on `/dev/sda2`,
   derives the volume key inside the guest, opens `root_crypt`, and shreds the
   plaintext key;
2. `odile-firstboot.service` exchanges `odile.token` for the signed seed at
   `odile.seed_url`, verifies the Ed25519 signature, caches the pair 0600, and
   runs the release installer; the machine's `/usr/local/bin/odile-install.sh`
   and its `/manifest` pair are the release's own bytes (ODL-472);
3. the first-visit page answers on loopback; Caddy serves
   `https://<slug>.odile.fyi` with a certificate whose SHA-256 fingerprint the
   done page writes into the recovery file;
4. a second boot prints `same`, spends nothing and touches no token.

The one console line is the contract: `odile-firstboot: installed … ·
first-visit 200 · Ns`, or a single `fail-closed: <reason>` line with the
machine still reachable (`deploy/image/FIRSTBOOT.md` §2).

**The zero-touch image needs neither `scp` nor a passphrase.** On a tenant
image built from `release/` (ODL-456/ODL-472) the first boot places the
release's exact installer at `/usr/local/bin/odile-install.sh` (0755 root), the
signed pair at `/etc/odile/manifest.json`/`.sig`, and the loopback listener,
before it exchanges the seed or runs anything. A tenant therefore satisfies
CLAIMS §4.6 by reading the file, with no copy step:

```sh
sha256sum /usr/local/bin/odile-install.sh        # == the manifest's installer_sha256
curl -sD- https://<slug>.odile.fyi/manifest      # the signed release manifest, no-store
```

The image refuses to build unless the manifest names the installer it carries
and both signatures verify (`sealed-image.sh`); the first boot refuses to place
a pair the published key does not verify.

### 12.5 What the machine has proved on 2026-11-11 (before the panel)

- **The measured command line, freshly.** A 2026-11-11 boot prints exactly one
  `bb.key`, decoding to the owner's key, with `bb.release=2026.11.2` and
  `bb.iid=01494715-44ac-45d0-a107-4f7ed600676e`. The 2026-11-09 baseline
  (`2026.11.1`, `SHA256:DGAw…`, `7448afca…`) is gone.
- **One host key.** `SHA256:fFic3fm3N0kx5c8MC4SaJugl7pRusjaR70Gdr/G2WgM` is the
  value `tenant_state.initramfs_host_key` records, and a restart does not
  change it (2026.11.2 seals it to the guest on `/dev/sda2`).
- **No operator login.** `ssh -F /dev/null -o BatchMode=yes` from HQ exits 255
  (`Permission denied (publickey)`).
- **The disk is untouched.** The boot banner is "this disk is not encrypted
  yet" and nothing serves 80/443, so the panel's Disk image field is what puts
  the sealed disk and its boot chain there.
- **The provider's `fde_enabled` and `measured_release` are not the boot's
  state.** They read `true` / `2026.11.1` while the boot says the disk is
  unencrypted and `bb.release=2026.11.2`; read the serial, not the field.

### 12.6 The published release is the signed installer

Re-cut on 2026-11-11 as release `2026.11.11-5` from a fresh scratch root the
merged installer made (`docs/runbooks/update.md` §5), and published by
`./scripts/deploy.sh` from the branch that carries it. The bundle is cut from that
branch's signed pair, so the download a stranger gets is the installer the
signature covers — and the tarball carries `odile-recipes`. Measured over TLS
immediately after the deploy, and re-hashed at 2026-11-12T05:12Z, when the URL
served exactly these bytes:

```sh
$ curl -s https://odile.fyi/odile_install.py | sha256sum
fde7a48fa2675d63b57a1e6bd6917c2fab83b1d7a9d18c34beb1854c3e9c3fa6  -
$ sha256sum installer/odile_install.py
fde7a48fa2675d63b57a1e6bd6917c2fab83b1d7a9d18c34beb1854c3e9c3fa6  installer/odile_install.py
$ curl -s https://odile.fyi/release.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["artifacts"]["installer"])'
{'sha256': 'fde7a48fa2675d63b57a1e6bd6917c2fab83b1d7a9d18c34beb1854c3e9c3fa6', 'size': 553042, 'url': 'odile_install.py'}
$ curl -s https://odile.fyi/skills.tar.gz | tar -tzf - | grep odile-recipes
odile-recipes/SKILL.md
```

All nine names the route then served — `release.json`, `release.json.sig`,
`manifest.json`, `manifest.json.sig`, `odile_install.py`,
`odile_install.py.sig`, `odile-signing-key.pub`, `meter.py`,
`skills.tar.gz` — came back byte-identical to the `release/` of that
`2026.11.11-5` cut (`cmp` on each name), and its `release/odile_install.py` is
byte-identical to its `installer/odile_install.py`. The served manifest's
`installer_sha256` is that same digest. The tree has since cut `-8` (its own
pair, below); the URL is what a stranger downloads, and at that 2026-11-12T05:12Z
re-hash it was still `-5` (the live value is measured at the end of this section).

The installer is now the file the release names. On the fresh scratch root
this release's installer made (`installer/sim_tenant.py --reset --apply`,
22/22; run 2 reads `tenant: same` with no writes), the copy the install writes
for itself is the manifest's digest — the CLAIMS §4.6 command, as written:

```sh
$ sha256sum /var/tmp/odile-470-sim/usr/local/bin/odile-install.sh
fde7a48fa2675d63b57a1e6bd6917c2fab83b1d7a9d18c34beb1854c3e9c3fa6  /var/tmp/odile-470-sim/usr/local/bin/odile-install.sh
$ curl -s https://odile.fyi/manifest.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["installer_sha256"])'
fde7a48fa2675d63b57a1e6bd6917c2fab83b1d7a9d18c34beb1854c3e9c3fa6
```

The cut is reproducible: the manifest rebuilt from that root at the recorded
timestamp is `0bbc91a32c84…`, and `release` over it reproduces every one of the
nine published names byte for byte (`docs/runbooks/update.md` §5). The
published bundle is a function of the signed pair and the candidate root's app
layer, not of one particular scratch root.

The bundle also carries the signature and the key (`docs/runbooks/update.md`
§3), so the whole ritual — step 0 and step 1 of §12.2 — runs from the three
release URLs, with no repository:

```sh
$ curl -sO https://odile.fyi/odile_install.py
$ curl -sO https://odile.fyi/odile_install.py.sig
$ curl -sO https://odile.fyi/odile-signing-key.pub
$ sha256sum odile-signing-key.pub
13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2  odile-signing-key.pub
$ python3 odile_install.py --verify-signature --pubkey odile-signing-key.pub
signing key  sha256 13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2 · SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8
signature: OK (sha256 fde7a48fa2675d63b57a1e6bd6917c2fab83b1d7a9d18c34beb1854c3e9c3fa6)
```

The served `release.json` and `manifest.json` verify against the same key, and
the served manifest's `installer_sha256` is that same digest — the guide's
`--verify-signature` step now passes on bytes the release names, not on a repo
checkout that happens to be newer. Re-cutting is `docs/runbooks/update.md` §5
from a root the current installer made; **do not re-sign the installer** to
rebuild a bundle. A cut is stale the moment the installer moves: ODL-471 and
the tenant template's `reasoning_effort` (`6dfb682`) changed the bytes after
`2026.11.09-1`, ODL-332's follow-up (the self-copy at
`/usr/local/bin/odile-install.sh`) moved them again after `2026.11.11-1`,
ODL-473's `/verify` surface moved them again and cut `2026.11.11-3`,
ODL-472's `/manifest` routes and first-boot placement moved them again after
`2026.11.11-3`, and ODL-470's strict-bar help surface moved them again after
`2026.11.11-4` — `2026.11.11-5` is the cut from the head that carries every
strict-bar surface, and was what the URL served after the deploy from that
branch. The ODL-471 follow-up's refused page moved them once more after
`2026.11.11-5`, so the cut from that head is `2026.11.11-6`. The shell scale on
`/verify` and the first-visit page (ODL-243 S1b) moves them again; its first cut
on the branch was `2026.11.11-7`, taken before `main` moved and never served,
and the cut from the merged head is `2026.11.11-8`. Each cut is made from the
head that merges; the archive's installer hash is the checkout's at that moment.

The tree cut `2026.11.12-4` on that merged head: ODL-208's handoff audit,
fixed by its follow-up to read sshd's second `authorized_keys2` file and to
scan `/root`, `/home`, `/etc/odile` and `/var/lib/odile` for the boot key's
private half, moves the installer once more. The manifest is `2026.11.12-3`'s
root-built manifest with only `audit_sha256`, `installer_sha256` and
`generated_at` moved — the diff against the `-3` document is exactly those
three lines, so `-6`'s fresh-root run stays the last full rebuild.
`release/odile_install.py` is the checkout's
602,968-byte installer and `release/odile_install.py.sig` its detached
signature; `release.json` names installer `2405fd58…` (602,968 bytes) and
manifest `70887d8a…`, and both JSON signatures verify against
`installer/odile-signing-key.pub`. Measured 2026-11-13T04:51Z: the URL serves
this cut, `2026.11.12-4` (installer `2405fd58…`, 602,968 bytes; manifest
`70887d8a…`).

ODL-462 re-cuts `2026.11.12-5` on *its* merged head. Its v3.9 claims landing
re-cuts the gap 2 and gap 3 blocks of `web/verify.html`, and that page travels
inside the signed installer (`installer/embed_verify.py`), so the installer
moves from `2405fd58…` to `42ca1846…` (603,046 bytes). The manifest is the
`-4` document with only `installer_sha256` and `generated_at` moved — the
audit is still `539e205f…` and every skill row is unchanged, so the diff
against the `-4` document is exactly those two lines. `release/release.json`
names installer `42ca1846…` (603,046 bytes) and manifest
`6e6a6df3…`; `release/odile_install.py` is the checkout's installer,
`release/odile_install.py.sig` its detached signature, and both JSON
signatures verify against `installer/odile-signing-key.pub`. It is committed,
not deployed. Measured 2026-11-13T04:51Z: the URL still serves `2026.11.12-4`
(installer `2405fd58…`, 602,968 bytes; manifest `70887d8a…`); this cut
publishes when a `./scripts/deploy.sh` runs from a `main` that carries it. The release
id must not be reused; ODL-497 takes the next cut, `2026.11.13-2`, below.

ODL-497 re-pins the vendor script and re-cuts `2026.11.13-2` on the merged
head, which also carries ODL-462's v3.9 claims. The pin moves to the vendor's
2026-11-13 script (67,104 bytes, `50348fcc…`; the one change is `tar -o`,
§4b), and the merged installer — ODL-462's `/verify` page plus the new pin —
is `afdd50b6…` (603,253 bytes). The manifest is a full fresh-root build, not
a surgical rebuild: `sim_tenant.py --reset --apply` on `/var/tmp/odile-497b-rc`
ran 22/22, run 2 read `tenant: same` with no writes, and the build from that
root recorded the pinned commit `2bd378ed1b8d` beside
`hermes.install_script_sha256 50348fcc…` and installer digest — `b48549e8…`
(50,043 bytes, 48 skills). `release/release.json` names installer `afdd50b6…`
(603,253 bytes) and manifest `b48549e8…`; `release/odile_install.py` and its
`.sig` are the checkout's pair, and both JSON signatures verify against
`installer/odile-signing-key.pub`. A second cut into a scratch directory
reproduced all nine names byte for byte. It is committed, not deployed.
Measured 2026-11-13T05:48Z, the URL serves `2026.11.12-4` (installer
`2405fd58…`, 602,968 bytes; manifest `70887d8a…`), the deployed pair, one cut
behind `main`'s `2026.11.12-5` (installer `42ca1846…`, 603,046 bytes; manifest
`6e6a6df3…`); the next `./scripts/deploy.sh` from a `main` that carries this merge
publishes `2026.11.13-2`. ODL-496 takes the next cut, `2026.11.13-3`, below;
the ODL-215 follow-up then cuts `2026.11.13-4`.

ODL-496 re-cuts the tenant's own standing rules and cuts `2026.11.13-3` on the
merged head. `templates/tenant-agents.md` is installed as `$HOME/AGENTS.md`,
and its reboot answer was still the v3.7 row that v3.9 cut ("The machine waits
for its owner. Your key gets you in, and your passphrase unlocks the disk.");
it now quotes A5 and A4's no-passphrase sentence verbatim from
`docs/CLAIMS.md` — "After a reboot, it starts itself. The seal is the key." and
"There is no passphrase to type." The file travels inside the installer, so
`installer/embed_tenant.py` re-embedded it (`TENANT_AGENTS_SHA256 0df62439…`
→ `9c28c1fe…`), the installer was re-signed (`04ca5137…`, 603,247 bytes) and
the release re-cut from a fresh scratch root the merged installer made
(`sim_tenant.py --reset --apply` on `/var/tmp/odile-496-cand`, 22/22; run 2 read
`tenant: same` with no writes). The manifest is the `-13-2` document with only
`installer_sha256` and `generated_at` moved — `ade1a766…` (50,043 bytes, 48
skills, audit `539e205f…`); `manifest.py diff` against the committed `-13-2`
document prints that one line. `release/release.json` names installer
`04ca5137…` (603,247 bytes) and manifest `ade1a766…`; `release/odile_install.py`
and its `.sig` are the checkout's pair, and both JSON signatures verify against
`installer/odile-signing-key.pub`. On that fresh boot `$HOME/AGENTS.md` is
byte-identical to `templates/tenant-agents.md` (`cmp` exit 0), and the tenant
agent's answer to "How do I get back in after a reboot?" was "After a reboot,
it starts itself. The seal is the key. There is no passphrase to type." A
second fresh root installed from the release copies (`/var/tmp/odile-496-rel`)
ran 22/22, run 2 `tenant: same`. It is committed, not deployed. Measured
2026-11-13T07:57:53Z, the URL serves `2026.11.12-4` (installer `2405fd58…`,
602,968 bytes; manifest `70887d8a…`); the next `./scripts/deploy.sh` from a `main` that
carries this merge publishes `2026.11.13-3`. The release id must not be reused;
the ODL-215 follow-up takes the next cut, `2026.11.13-4`, below.

```sh
$ date -u +%Y-%m-%dT%H:%M:%SZ
2026-11-13T05:48:32Z
$ curl -fsSL https://odile.fyi/release.json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["release"],d["generated_at"],d["manifest"]["sha256"],d["artifacts"]["installer"]["sha256"],d["artifacts"]["installer"]["size"])'
2026.11.12-4 2026-11-12T23:43:26Z 70887d8a3716e5229e1ac552a0aadfa602f0f232325a1f91e9d5ada2ec4ab04a 2405fd5830f4adb2a5af3de5a9a4577c6c66c00c002766f3b67f000e33826123 602968
$ curl -fsSL https://odile.fyi/odile_install.py | sha256sum
2405fd5830f4adb2a5af3de5a9a4577c6c66c00c002766f3b67f000e33826123  -
$ curl -fsSL https://odile.fyi/manifest.json | sha256sum
70887d8a3716e5229e1ac552a0aadfa602f0f232325a1f91e9d5ada2ec4ab04a  -
```

A first boot from this cut is proved by running the **release copies** through
the same harness on a second fresh root (`/var/tmp/odile-497b-rel`): 22/22,
run 2 `tenant: same`; its run 1 log prints `install.sh 67104 bytes, sha256
50348fcc95e9… matches the pin`, its Hermes stamp names `2bd378ed1b8d`, and the
log carries no pin refusal. `docs/runbooks/update.md` §5 carries the full cut
transcript.

This tree cuts `2026.11.13-4` on the merged head, which carries ODL-497's
vendor pin, ODL-496's tenant standing rules and the ODL-215 follow-up's backup
placement: the installer embeds `scripts/backup.py`, places it at
`/usr/local/lib/odile/backup.py` (0755 root), writes
`odile-backup-tenant.service` and `.timer`, enables the timer on a tenant, and
writes the heartbeat URL the receiver answers (`https://odile.fyi/heartbeat`).
The installer moves from `04ca5137…` (603,247 bytes) to `8ab356cf…` (654,180
bytes). The manifest is the `2026.11.13-3` document with only
`installer_sha256` and `generated_at` moved — `b5730dae…` (50,043 bytes, 48
skills, audit `539e205f…`); a plain `diff` against the committed `-3` document
prints exactly those two lines, so the app layer is the `-3` build's.
`75a21aa` then removed the three live addresses from the tree and re-signed the
same release in place: the `-4` pair `main` carries is installer `b8f803c6…`
(654,180 bytes) and manifest `70579326…`, and `release/release.json` names those
two. `release/odile_install.py` is the checkout's installer,
`release/odile_install.py.sig` its detached signature, and both JSON signatures
verify against `installer/odile-signing-key.pub`. A second cut into a scratch
directory reproduced all nine names byte for byte. It is committed, not
deployed. Measured 2026-11-13T12:10Z: the URL still serves `2026.11.12-4`
(installer `2405fd58…`, 602,968 bytes; manifest `70887d8a…`); this cut
publishes when a `./scripts/deploy.sh` runs from a `main` that carries it. The release
id must not be reused: ODL-514 took `2026.11.13-5` on its own branch and
ODL-504 re-cuts `2026.11.13-6` on top of this pair, below. A first boot
from this cut is proved on a fresh QEMU bench guest in
`docs/runbooks/backup.md` §2.1: the release installer placed the program and
both units, `systemctl is-enabled odile-backup-tenant.timer` printed `enabled`,
one unit run wrote `/var/lib/odile/backup.json`, and the production heartbeat
route accepted the post.

This tree re-cuts `2026.11.13-6` on the merged head that carries ODL-215's
backup placement, `main`'s re-signed `-4` pair and ODL-504's website icon.
ODL-504 puts the icon links on every page under `web/`, `/verify` and the
first-visit page included, and those two pages travel inside the signed
installer: `installer/embed_verify.py` and `installer/embed_first_visit.py`
re-embedded them (`VERIFY_PAGE_SHA256 8814faea…` → `0d16c494…`,
`FIRST_VISIT_PAGE_SHA256 f8718ea4…` → `4d7c4960…`) and the merged installer was
re-signed. It moves from `b8f803c6…` (654,180 bytes) to `ef559548…` (654,258
bytes). The manifest is the re-signed `-4` document with only
`installer_sha256` and `generated_at` moved — `881bc481…` (50,043 bytes, 48
skills, audit `539e205f…`); `scripts/manifest.py diff` against the committed
`-4` document prints exactly those two lines. `release/release.json` names
installer `ef559548…` (654,258 bytes) and manifest `881bc481…`;
`release/odile_install.py` is the checkout's installer,
`release/odile_install.py.sig` its detached signature, and both JSON signatures
verify against `installer/odile-signing-key.pub`. The cut came from a scratch
root the merged installer made (`sim_tenant.py --reset --apply` on
`/var/tmp/odile-504c-cand`, 22/22; run 2 read `tenant: same`), with the manifest
built at 2026-11-13T19:48:29Z. The vendor's `install.sh` moved after ODL-497's
pin (the URL served 68,015 bytes, `c73dad4d…`, at cut time), so that run fed the
installer the pinned 67,104-byte `50348fcc…` copy kept on HQ; the installer's
own pin check still ran and `hermes.install_script_sha256` is unchanged. It is
committed, not deployed. Measured 2026-11-13T19:52Z, the URL serves ODL-514's
`2026.11.13-5` (installer `6bcb82eb…`, 664,069 bytes; manifest `f4a7ddfd…`), so
the next `./scripts/deploy.sh` from a `main` that carries this merge publishes `-6`. The
branch's first cut of this head was `2026.11.13-5` (`e943d11a…`) before `main`
moved; ODL-514 took and deployed that id, so it must not be reused and the next
cut after `-6` is `-7` or later. ODL-514 took and deployed `-7`; the next cut
after it is `-8` or later, and this card cuts `2026.11.14-3` below because
`-8` and `-9` are carried by open branches and `2026.11.14-2` was taken and
deployed by t_9c2d31f1's HQ `/verify` cut.

**The `2026.11.13-7` cut (ODL-514's rework).** This tree merges `main`'s `-6`
head into the seed-key split and cuts `-7`. The installer moves from
`ef559548…` (654,258 bytes) to `6881403b…` (664,147 bytes) because it now walks
the seed certificate: the release key certifies the seed key a seed carries, and
the body is verified against that certified key (pre-ODL-514 seeds with no
certificate still verify against the release key). The first boot in the image
walks the same chain (`deploy/image/odile-firstboot.py`), and the image bakes
the retired key's public half beside the current one for the release that moves
between them. The manifest is the `-6` document with only `installer_sha256` and
`generated_at` moved — `e23ce99a…` (50,043 bytes, 48 skills, audit
`539e205f…`); `diff` against the committed `-6` document prints exactly those
two lines. `release/release.json` names installer `6881403b…` (664,147 bytes)
and manifest `e23ce99a…`; `release/odile_install.py` is the checkout's
installer and `release/odile_install.py.sig` its detached signature; both JSON
signatures verify against `installer/odile-signing-key.pub`. The seed
certificate `deploy/seed/seed-key.crt` and the seed public key
`deploy/seed/seed-key.pub` are unchanged and still verify against that key. The
cut was made at 2026-11-14T01:41:33Z. It is deployed: measured
2026-11-14T02:21:43Z, `https://odile.fyi/release.json` serves `2026.11.13-7`
and `https://odile.fyi/odile_install.py` hashes to `6881403b…`; the deploy
from this head (which contains `main` `37372d4`) printed the guard line and
restarted `odile-seed` on the new credential path.

**The `2026.11.14-1` cut (t_e237e261, the meter removal).** The installer no
longer carries or installs an inference meter: its embedded meter, unit, user
and `--meter-port` are gone; the seed's per-user NEAR key is written to
`/etc/odile/key.env` (0600 root) and `odile-agent.service` reads it through
`EnvironmentFile=`; the tenant config is `provider: custom` at
`https://cloud-api.near.ai/v1` with one `custom_providers` row named `Near AI`.
The installer moves from `6881403b…` (664,147 bytes) to `f211738c…` (616,222
bytes); the manifest is the `-7` document with `meter_sha256` removed,
`installer_sha256` and `generated_at` moved and the `odile-home` skill hash
replaced (the one skill the installer writes changed); the skills tarball is
`-7`'s bytes with that one canonical file replaced (`09be3cd1…`, 851,865
bytes), and `release/release.json` no longer lists a meter artifact
(`8f14c641…`, manifest `381de4e6…`). Both JSON signatures verify against
`installer/odile-signing-key.pub`. The cut is surgical because the upstream
Hermes `install.sh` moved today (68,015 bytes, `c73dad4d…`, against the pinned
50,348 bytes, `50348fcc…`), so no root was rebuilt; only the installer hash and
the canonical `odile-home` skill moved. It is **committed, not deployed**:
`https://odile.fyi/release.json` still serves `2026.11.13-7` until the next
`./scripts/deploy.sh` from a `main` that carries this merge.

**The `2026.11.14-3` cut (the second vendor re-pin).** This tree merges
`main`'s meter-free `0a4b726` head (`2026.11.14-1`, t_e237e261) into the pin
change and cuts `2026.11.14-3`. `-1` was a surgical cut that left the old pin
in place because the upstream script had moved, so a fresh install still
refused; this cut rebuilds from a fresh root and moves the pin. The installer's
pin moves from `50348fcc…` (67,104 bytes) to `c73dad4d…` (68,015 bytes);
against the 2026-11-13 script the whole diff is git-location hygiene —
`drop_git_location_env` in `stage_repository`/`stage_complete` and a
`GIT_CEILING_DIRECTORIES` fence on the broken-`.git` probe (#146032; §4b) —
with no new command, URL, `eval` or privilege. The re-signed installer is
`fd405391…` (616,445 bytes: t_e237e261's meter-free installer plus the pin).
The manifest is a full fresh-root build: `sim_tenant.py --reset --apply` on
`/var/tmp/odile-t_19642a84-rc2` fetched `install.sh 68015 bytes, sha256
c73dad4d… matches the pin`, ran 24/24 (the meter-free harness replaced the
meter assertion with three per-user-key checks), and run 2 read `tenant: same`
with no writes. `scripts/manifest.py diff` against the committed `-1` document
prints exactly two lines — `hermes.install_script_sha256` and
`installer_sha256`; the document is `d0f35d7b…` (49,957 bytes, 48 skills,
audit `539e205f…`). `release/release.json` names installer `fd405391…`
(616,445 bytes) and manifest `d0f35d7b…`; `release/odile_install.py` and its
`.sig` are the checkout's pair, and both JSON signatures verify against
`installer/odile-signing-key.pub`. A second cut into a scratch directory
reproduced all eight names byte for byte. It is committed, not deployed.
Measured 2026-11-14T07:58Z, the URL serves `2026.11.14-2` (t_9c2d31f1's HQ
`/verify` cut, installer `bac5e058…`, 616,145 bytes; manifest `0bcc735d…`),
which still pins `50348fcc…`; the next `./scripts/deploy.sh` from a `main` that
carries this merge publishes `2026.11.14-3` — except that `-4` below superseded
it before any deploy. The id is the next free one:
`-7` and `-2` are deployed, `-8` and `-9` are carried by open branches, and
this cut's `generated_at` (2026-11-14T07:57:20Z) is later than every live cut,
so `odile-update` orders it after them.

**The `2026.11.14-4` cut (t_9c2d31f1's `/verify` re-embed on the vendor
re-pin).** HQ's `/verify` page still published the retired demo-reset note; the
demo moved to the tenant at ODL-503 and `docs/CLAIMS.md` v3.8b says "There is
no nightly reset", so `web/verify.html` drops the note. The page travels inside
the signed installer, so this branch re-embedded it on the merged head that
carries `-3` (`installer/embed_verify.py`; page pin `8b1a3198…`, the same page
`-2` carried) — the merged installer is `fc723909…` (616,368 bytes: `-3`'s
vendor pin plus the page block). The manifest is `-3`'s document with only
`installer_sha256` and `generated_at` moved — `fcf0bc61…`; `diff` against the
committed `-3` document prints exactly those two lines, so the vendor pin
`c73dad4d…`, the skills artifact and the meter-free rows are unchanged.
`release/release.json` names installer `fc723909…` (616,368 bytes) and manifest
`fcf0bc61…`; `release/odile_install.py` is the checkout's installer and
`release/odile_install.py.sig` its detached signature; both JSON signatures
verify against `installer/odile-signing-key.pub`. The cut was made at
2026-11-14T09:32:15Z. It is deployed: measured 2026-11-14T09:48Z,
`https://odile.fyi/release.json` serves `2026.11.14-4`,
`https://odile.fyi/odile_install.py` hashes to `fc723909…` (616,368 bytes), and
HQ's `/verify` carries no "resets daily".

**The `2026.11.14-5` cut (ODL-521, the image's sign-in).** This tree merges
the new single-root `main` (8e37ec3) into ODL-521's sign-in slice and cuts
`-5` on that head. The installer carries both: the published image's `.env`
holds a discarded scrypt hash behind `# odile-signin-placeholder: 1`, the
first boot leaves it alone, and `ensure_env(first_visit=True)` rewrites the
hash on every boot until the first-visit page records `signin_set_at`. The
installer moves from `6b42f8ef…` (616,370 bytes) to `10a38b68…` (617,829
bytes). The manifest is the `-4` document with only `installer_sha256` and
`generated_at` moved (`daa8340f…`); the vendor pin, the skills artifact
`09be3cd1…` (851,865 bytes, the canonical `<home>/workspace` cut) and the
meter-free rows are unchanged. `release/release.json` names installer
`10a38b68…` (617,829 bytes) and manifest `daa8340f…` (`c6fa1ed1…`). Both JSON
signatures verify against `installer/odile-signing-key.pub`, and
`tools/recut_release.py --check` prints `ok` for the installer, its release
copy, the manifest, the release document and the pin's hook. It is
**committed, not deployed**: measured 2026-11-14T15:25Z,
`https://odile.fyi/release.json` serves `2026.11.14-4` until the next
`./deploy.sh` from a `main` that carries this merge.

The clock starts at the build's `uki.sh --sign` and stops when
`https://<slug>.odile.fyi/login` answers 200 over TLS. The gate is 30 minutes.
Round 8 is the measured stop: the panel was saved at 14:42:53Z, the guest's
kernel booted at 14:43:54Z, the first-visit page answered at 14:44:45Z and the
release half finished at 14:46:43Z — **112 s from the save to the page, 2m50s
to the release done**. Every DoD line of the strict run now passes on the live
machine (`/manifest`, the done page's recovery download and the two drills
included); [`docs/evidence/m1-tenant.md`](../evidence/m1-tenant.md) §9.8 and
§10 carry the transcript and the leg table. The machine's own path from kernel
to the first-visit page is 17–26 s on every installed boot; the
panel-to-restart latency is the rest. The simulation's 3m22s (warm) and 14m47s
(cold runtime) are not a tenant measurement.
