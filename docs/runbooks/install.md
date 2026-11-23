# The installer — one signed command, run by the user

ODL-203. `installer/odile_install.py` turns a freshly booted Debian VM into a
working Odile tenant. The **user** runs it as root; Odile never logs in, holds no
operator key and never sees the sign-in password.

The tenant install is one extraction: the scrypt hash, the `.env` allowlist, the
managed-block config merge, the atomic writes, the Hermes install, the Webapp
build and the service handling live in the installer. The demo tenant
(`odl-fake01`) runs this same signed installer, so the demo and a customer
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
| `installer/embed_support.py` | refreshes the embedded support programs (`odile-support`, `odile-diag`) and their pinned SHA-256; the only supported way to change them (ODL-474) |
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
`public_url https://<slug>.<the seed endpoint's host>` — `https://<slug>.odile.fyi`
in production, `<slug>.test.odile.fyi` when the seed is
`https://test.odile.fyi/seed`. `--public-url` names a site the environment's
shape does not (a moved name, §12.4).

The tenant install also takes the boot assertion: `--expect <96-hex>` (the
measurement the verifier printed), optionally `--release <id>` to assert the
release id, and `--boot-report PATH` when the report is not at the tenant
default `/run/blackbox/attest.json`. §3a is the contract. The nightly backup
posts its signed heartbeat to the environment that issued the seed
(`<seed origin>/heartbeat`; production is `https://odile.fyi/heartbeat`, the
receiver's route in `backup.md` §2); `--heartbeat-url` names another receiver.

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
4. **Odile sign-in.** The password is the one *you chose at registration*: the
   signed seed carries its scrypt verifier (`sign_in_hash`), and the installer
   writes it into `.env` before the Webapp's first boot — so the basic-auth
   gate accepts that password with no page visited, and the first visit is a
   login. The best time to change it is **before the first sign-in**: changing
   it at the account page afterwards re-keys the account, not this machine.
   A classic install with no seed still asks for *your Odile sign-in* here —
   16+ characters, or blank to generate 32 and print it once — and hashes it on
   the machine with scrypt (`scrypt$n$r$p$salt$dk`, the dashboard provider's own
   format); only the hash reaches `.env`, and nobody can sign in until the page
   writes the owner's hash. Either way, Odile sees your password when you
   register and when you sign in at the account page and keeps only a hash of
   it. Your machine is built with a copy of that hash; after that, changing one
   does not change the other.
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
   `odile-smoke`, every 5 minutes, no agent, delivering locally. A 401 in the
   chat row is an ordering between the boot's own gateway and
   `/etc/odile/key.env`, not an expired key — §10 has the one command that
   tells them apart.

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
| `/home/odile/.hermes/config.yaml` | 0644 | odile | seven managed blocks: model, agent, custom_providers, dashboard, webapp, onboarding, auxiliary; `dashboard.public_url` and `webapp.public_url` name the host Hermes's Host check accepts (t_d95b8c13); `auxiliary.vision.model` pins a vision model in NEAR AI's Private TEE tier, chosen because its answer is not eaten by a reasoning phase (ODL-341-F1); Hermes's own keys are preserved; `display.skin: odile` (§4a) |
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
| `templates/config.yaml` | `/home/odile/.hermes/config.yaml` | the `display.skin: odile` block, merged beside the seven managed blocks; every other display key is kept |
| `templates/webapp-brand.js` | `<dist-webapp>/odile-brand.js` plus a `<script>` tag in `index.html` | the strings a skin cannot reach: the page title, the empty-state wordmark and its accessible name, the rotating composer prompt, and the About credit line |
| `deploy/image/odile-webapp-brand.py` | `/usr/local/lib/odile/odile-webapp-brand.py` (0755) | the re-assert: the same splice and the same three tab links, from the same bytes, whenever the bundle changes |
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
keeps the owner of the shell it patches. The same run puts the tab back
(ODL-505): the vendor's three icon links return after a rebuild, so the helper
copies `favicon.ico` and `apple-touch-icon.png` from the installer's icon root
(`/usr/local/lib/odile/icons`) into the bundle as `odile-favicon.ico` and
`odile-apple-touch-icon.png` and repoints the links — the vendor's own
`apple-touch-icon.png` stays where it is. A machine whose icon root is missing
(installed before ODL-505) keeps the build's links and prints `wait`; the brand
splice still runs. The build also converges the image itself, so a fresh
machine never serves the vendor's name even once.

```sh
$ journalctl -u odile-webapp-brand.service -n 5
  write        Webapp brand (index.html linked, odile-brand.js vf46b92de7db1, icons odile-favicon.ico odile-apple-touch-icon.png)
  same         Webapp brand (odile-brand.js vf46b92de7db1, index.html linked, 2 icon files)
$ grep -c 'odile-brand.js' \
      /home/odile/.hermes/hermes-agent/apps/desktop/dist-webapp/index.html
1
$ grep -c 'href="./odile-' \
      /home/odile/.hermes/hermes-agent/apps/desktop/dist-webapp/index.html
3
```

A converged home prints `same` for all three installer steps; a bundle that is
missing is `wait` (the path unit fires when the build creates it), and a shell
without `<head>` or `</html>` is left alone until the build finishes it.

**What is not themed, and why** (ODL-240: list it, leave it):

- **`GET /login`** — the gateway's server-rendered sign-in page: vendor amber
  `#ffac02`, "Nous Research", "Hermes Agent dashboard", and the horse favicon.
  v0.23.0 exposes no template or config seam for it, and replacing it would
  fork the auth page rather than skin it. It is the one screen before the
  product that still looks like the vendor's admin panel, and the one surface
  that still shows the vendor's tab icon; every screen after sign-in is
  Odile's. (The demo host's own sign-in page is Odile's page and links the
  same three icon paths as `/verify`, served first-party; a tenant's `/login`
  is the gateway's, not ours — ODL-505.)
- **The About hero, the model/effort labels and the About brand mark** —
  vendor copy and a vendor mark, kept. The About hero's "Powered by Hermes" is
  the credit the tab used to carry: ODL-505 points the shell's three icon links
  at Odile's own files, so the About hero is where the vendor is now named.
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
$ grep -n 'rel="\(icon\|apple-touch-icon\|shortcut icon\)"' \
      /home/odile/.hermes/hermes-agent/apps/desktop/dist-webapp/index.html
    <link rel="icon" sizes="16x16 32x32 48x48" href="./odile-favicon.ico" />
    <link rel="apple-touch-icon" sizes="180x180" href="./odile-apple-touch-icon.png" />
    <link rel="shortcut icon" href="./odile-favicon.ico" />
$ ls -l /usr/local/lib/odile/icons/    # the tenant page server's own set
-rw-r--r-- 1 root root  11790 … favicon.ico
-rw-r--r-- 1 root root   2302 … icon-32.png
-rw-r--r-- 1 root root  69759 … icon-192.png
-rw-r--r-- 1 root root 410903 … icon-512.png
-rw-r--r-- 1 root root  47543 … apple-touch-icon.png
$ curl -sI https://<slug>.odile.fyi/favicon.ico | head -4
HTTP/2 200
content-type: image/x-icon
cache-control: public, max-age=31536000, immutable
```

Open the Webapp: the tab reads `Odile`, the empty state reads `Odile`, and
Settings → About shows `Hermes`, `Version <x.y.z>` and `Powered by Hermes`.
`--manifest` carries the skin and brand hashes so an audit can tie the running
files to the signed installer.

**Known limits.** The skin engine clips `customCSS` to 32 KiB, so the embedded
Cormorant face used on the landing page cannot travel inside the skin. The
heading stack is the system serif — `"Iowan Old Style", "Palatino Linotype",
Palatino, Georgia, "DejaVu Serif", serif` — the closest available face, and no
font host is contacted. Dark mode is not in alpha.0: the desktop converter
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
`2026.11.14-4` (installer `fc723909…`, 616,368 bytes; §12.6). t_781fc5cb's
v3.9f demo-lede re-embed then cut `2026.11.14-6` (installer `49f90a3c…`,
616,403 bytes; §12.6), which `./deploy.sh` published. ODL-521 then cut `-7`
on its branch (installer `ae2c839d…`, 617,862 bytes; §12.6) and merged it.
ODL-505 then re-embedded both pages again on the one-root tree and re-cut
`2026.11.14-8` at the merged head (§12.6). ODL-474's v3.10 landing, ported
onto that tree and merged with this head, re-embeds every surface and cuts
`2026.11.15-2`, then re-cuts `2026.11.15-4` at the head that merges ODL-522's
`-3` (§12.6). t_afb081be then re-embeds the support program and cuts
`2026.11.15-6` (§12.6). On `main`'s `2026.11.22-2` (ODL-469's brief-email cut),
t_ab95ec2a re-embeds the first-person help skill and cuts `2026.11.22-3`
(§12.6). t_509f85e4 then re-embeds §5's first-person fix at the merged head
and cuts `2026.11.22-4` (§12.6).

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
same         /home/odile/.hermes/config.yaml (6 managed blocks, sha256 …)
same         /home/odile/.hermes/skins/odile.yaml (skin odile)
same         /home/odile/.hermes/config.yaml (display.skin odile)
same         Webapp brand (odile-brand.js v…, index.html linked, 2 icon files)
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

Measured 2026-11-23 on HQ (this box) from the `2026.11.23-1` cut (t_fe2aee5c),
`simulation: PASS`:

```
$ /usr/bin/python3 installer/sim_tenant.py --reset --apply
reset        root /var/tmp/odile-sim and user odiletest
create       user odiletest (home /var/tmp/odile-sim/home/odiletest, no sudo)
seed         /root/.odile/installer-sim/seed.db · tenant odl-sim-203 · instance srv-odl-sim-203
listening    seed 127.0.0.1:8082
run          1: exit 0 in 4m 25s; log /var/tmp/odile-sim/evidence/run1.log
ok   the first run exits 0
ok   the first run provisioned
== sign-in gate (independent of the installer's own smoke)
ok   GET / without a session -> 302 to /login: 302
ok   GET /api/sessions without a session -> 401: 401
ok   POST /auth/password-login, wrong password -> 401: 401
ok   POST /auth/password-login, the Odile password -> 200: 200
ok   the sign-in sets four session cookies (__Host- over HTTPS, bare over HTTP): 4 cookie(s), 0 __Host-
ok   GET / with the session cookie -> 200 Webapp shell: 200 shell=True
ok   GET /api/sessions with the session cookie -> 200: 200
ok   the gate is 302/401/200 (deploy/demo-host/check_demo_host.sh shape): 302/401/200
ok   the gate has the 302/401/200 shape: 302/401/200 shell=True
ok   the config declares the public host the Webapp reads: webapp http://odile-sim.test:9140 · dashboard yes
== the guest export program (ODL-218 follow-up)
export       /var/tmp/odile-sim/usr/local/bin/odile-export mode 0o755 sha256 864604ae…
ok   the installer places /usr/local/bin/odile-export
ok   the placed odile-export is 0755: 0o755
ok   the placed odile-export is byte-identical to `odilectl export <slug> --script`: 3897 vs 3897 bytes
ok   one shell command + one file write: …/workspace/smoke.txt = ['odile-smoke-ok', 'odiletest']
ok   one cron job ran: …/workspace/cron-proof.log = 2026-11-23T00:38:24Z odile-cron-ok
ok   the per-user key is at /etc/odile/key.env, 0600: /var/tmp/odile-sim/etc/odile/key.env mode 0o600
ok   the gateway's process environment holds the key (the unit's EnvironmentFile): /proc/360607/environ
ok   the profile .env carries no inference key
ok   the recovery file carries the backup password
ok   the recovery file carries the fingerprint placeholder
ok   the recovery file is browser-only (no scp, shred or root@)
ok   the recovery file names the agent as the way to see it again
== seed exchange refusals
ok   a replayed token is refused: 410 {'error': 'token already used', 'reason': 'used'}
ok   an expired token is refused: 410 {'error': 'token expired', 'reason': 'expired'}
run          2: exit 0 in 0m 2s; log /var/tmp/odile-sim/evidence/run2.log
ok   the second run exits 0
ok   the second run prints `tenant: same`
ok   the second run changes nothing: 0 write line(s)
simulation: PASS (29 passed, 0 failed)
```

ODL-471 re-cut the recovery-file copy, so the four recovery lines read
`ok the recovery file carries the backup password`,
`ok the recovery file carries the fingerprint placeholder`,
`ok the recovery file is browser-only (no scp, shred or root@)` and
`ok the recovery file names the agent as the way to see it again`. The run above
is 29 checks: the meter is out of the installer (t_e237e261), the guest export
program has its own leg (ODL-218 follow-up), and the gate row names the file
that asserts the table, `deploy/demo-host/check_demo_host.sh`, not the
`deploy_demo_site.py` the printed label used to name (t_fe2aee5c). The
2026-11-08 transcript this block replaces showed the meter rows and 21 checks.
`--self-test` proves the shipped copy on a machine with no services, and the
full simulation is re-run by ODL-332 on a tenant.

Run 1's 4m25s here was a warm Hermes runtime; a colder run took 14m47s when
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

The installer's own record of the table. The pins and snapshot are unchanged
since the reviewed ODL-214 head. `installer_sha256` is computed from
`INSTALLER_PATH` (`__file__`), so `--manifest` always agrees with `sha256sum`
on the installer you ran — and it moves on every re-sign, so the excerpt below
is measured at this tree's head, which is rebased onto `main`'s `9ec0873`
(through `60e195e`, ODL-242's per-user key and ODL-346's gate change, and
`ff1c6b0`, t_55813b46's late-sentence follow-up merged 2026-11-22T08:26Z; the
newest release cut is still `2026.11.21-6`, t_df24b754's symlinked-root cut),
with this card's
branch, which re-cut the pair as `2026.11.22-2` (installer 1,557,751 bytes;
hashed 2026-11-22, after re-embedding `main`'s `581a9b88…` sender) and must be
re-cut whenever
`installer/odile_install.py` is re-signed.
`backup_sha256` is the pin embedded for `scripts/backup.py`; at this head the
two agree:

```sh
$ sha256sum installer/odile_install.py scripts/backup.py
87340eeeff8035e34b52ce2cca31a94203e60b59682b157c600f913a48daf8fb  installer/odile_install.py
d9c4278a0660650e7edc6b50847d96cdbb71ca7709c52402257e94a291a98955  scripts/backup.py
$ ./installer/odile_install.py --manifest
  "installer_sha256": "87340eeeff8035e34b52ce2cca31a94203e60b59682b157c600f913a48daf8fb",
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
signature: OK (sha256 36f1ecaf224f22b04feec416bbaf5a24d8b574cb291ecfd95fe048b0f52e85d2)
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
f55bdb52c0d6346ba580f7b95f6205816a011e26130d4352b9f884dd8071224b  /tmp/edited.py
$ /usr/bin/python3 /tmp/edited.py --verify-signature \
      --pubkey installer/odile-signing-key.pub --signature installer/odile_install.py.sig
signing key  sha256 13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2 · SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8
signature: BAD (sha256 f55bdb52c0d6346ba580f7b95f6205816a011e26130d4352b9f884dd8071224b)
$ echo $?
3
```

Recorded on 2026-11-14 and re-measured on 2026-11-23 from the installer at this
head (ODL-925's B-once re-cut, `2026.11.23-2`; over t_fe2aee5c's gate-label
fix, `2026.11.23-1`; over
`2026.11.22-7`, t_69a749f0's docstring fix on `main`'s `1ec389e`; over
`2026.11.22-6`, t_e7e5ac73's vision-pin cut after review
corrected the tier note, over `2026.11.22-5`, that card's pre-review cut, over
`2026.11.22-4`, t_509f85e4's support-grant help cut
after §5's first-person fix; over `2026.11.22-3`, t_ab95ec2a's
first-person §3 cut, over `2026.11.22-2`, ODL-469's brief-email cut, whose
`2026.11.22-1` was that card's pre-rebase cut, superseded before any deploy; over
`2026.11.21-6`, t_df24b754's symlinked-root
cut, over `main`'s `2026.11.21-5`, t_d55b0e47's `/verify`-unit cut, and through
its parent merge `4d88408` `2026.11.21-4`, t_87943990's
boot-key walk cut, whose `2026.11.21-2` is t_bf6092d7's gateway start-input cut
and whose `2026.11.21-1` is t_9574a1c8's help-routing cut; `2026.11.20-4` is
t_e3540242's dry-run-fixture cut) that
carries the handoff audit
(ODL-208's `/usr/local/bin/odile-audit`, re-cut by the follow-up that reads
`authorized_keys2` and scans `/root` for the boot key's private half) beside
the strict-bar help surface (ODL-470's five answers, whose bill answer states
alpha.0's no-price line since `2026.11.20-1`: t_080f8a83 re-cut
`skills/odile-help/SKILL.md` moment 4 to "The private alpha has no price: Odile
pays for the machine and the inference." and dropped the v5 `$49` line with the
held `$12` allowance, re-embedded as `09c301f2…` and shipped in the rebuilt
skills artifact), ODL-457's first-visit
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
ODL-504's re-embed of `/verify` and the first-visit page with the website
icon links on the merged head that also carries `main`'s re-signed `-4` pair
(`75a21aa`), ODL-505's icon set and page server (the five reviewed files
inside the installer, the five tenant routes, the Webapp's three tab links and
the re-assert helper) ported onto the one-root tree and re-cut there,
t_9c2d31f1's re-embedded `/verify` page, t_781fc5cb's demo-lede `/verify` page
(`bf4dda17…`), ODL-474's v3.10 landing (the five held access sentences on the
pages, the skill and the support email; the support program placed at
`/usr/local/sbin/odile-support` with `odile-diag` beside it), ODL-522's
per-machine session secret (the first boot mints a fresh one; the page keeps it
and refuses an `.env` with none), ODL-514's seed-certificate
chain (the release key certifies the
seed key; the first boot and the installer both walk it), t_ae833f3f's
re-cut of the two embedded tenant pages and the skin onto BRAND.md's type
roles (`/verify` and the first-visit form on the text serif, the display face
only on the wordmark and h1/h2/h3; `templates/skin.yaml` names the display
face on headings), the simulation's
page server started with the layout's own icon root (§12.6), t_afb081be's
re-embed of the support program with the session wrapper's signal-window fix,
t_d95b8c13's tenant-config wording (the Webapp reads `webapp.public_url`;
`dashboard.public_url` is not its fallback) and the `login_table` row that
asserts the four `__Host-` cookies, t_2efbd6e3's guest export placement
(`odilectl/offboard.py`'s `EXPORT_PROGRAM` at `/usr/local/bin/odile-export`,
0755), the ODL-340 follow-up's one site name (`render_caddy_site`,
`ensure_caddy_site`, `wait_for_dns`, `wait_for_acme` and
`certificate_fingerprint` all read `layout.fqdn`, so `--public-url` names
Caddy, the DNS and ACME waits and the certificate the recovery file pins) and
its seed-derived environment (`<slug>.<seed host>` for a tenant and
`<seed origin>/heartbeat` for the nightly unit; a production seed keeps
`<slug>.odile.fyi` and `https://odile.fyi/heartbeat`), t_3cd62ef9's two-URL
`/verify` note (the verifier fetches `/attestation` and that report's own VCEK
from AMD's KDS), this card's round 2 (the machine-free `--dry-run
--recovery-kit <kit>` plan reads the kit before it resolves the layout, so its
site line, the Webapp's `public_url` and the DNS/ACME wait name the kit's
machine rather than the `<slug>` placeholder — `--public-url` then `--slug`
still win), the self-test's whole-bundle hash (`8ea15e2`), t_7d51ffba's skip
link on both embedded pages (`<a class="skip" href="#main">` before
`<main id="main">`, hidden at `left:-9999px` until `:focus-visible`), and this
card's dry-run fixture carrying the scrub's own same-length stand-in
(`deadbeef`) in place of the box id, this card's gateway-key ordering
(`/etc/odile/key.env` is a start input, a moved unit is restarted rather than
started, and the running gateway's own `/proc/<MainPID>/environ` is read), and
t_9574a1c8's installed help (the frontmatter description and the load line name
a cost, a bill and a subscription question; moment 4's words are pinned above
them), t_87943990's find-based audit walk (the `/verify` page's boot-key row
enumerates with `find(1)` and reads each candidate's first 48 bytes with
`head(1)`, and the page starts the audit beside its other sources), this card's
follow-up that makes `find` follow a private root that is itself a symlink
(`-H`, so the fast path's candidate set is the Python fallback's; t_df24b754),
and t_d55b0e47's `/verify` unit (its sandbox keeps cryptsetup's lock writable —
`ReadWritePaths=/run` beside `ProtectSystem=strict` — so the LUKS row reads
PASS on a machine whose header is readable), and ODL-469's brief bundle
(`scripts/send_brief.py` and `templates/brief.txt` embedded and pinned, the
first written 0755 and the second 0644, and `/etc/odile/mail-ca.pem` written
from the seed's `mail_relay.tls_ca`; the `Morning brief` prompt ends with the
installed sender),
t_ab95ec2a's first-person help skill (the `odile-help` §3 answer says "ask me";
the deck and the two desk surfaces keep "the agent"), t_509f85e4's §5 fix (the
support-grant answer says "my memory and past prompts"; A10 and the three desk
surfaces keep "the agent's memory"),
t_69a749f0's docstring (the pure helpers live here; the demo host's live
deployer is `deploy/demo-host/apply_demo_public.py`, and the
`deploy_demo_host.py` the paragraph used to name is on no ref),
t_fe2aee5c's gate label (the printed install result line, `gate_ok`'s
docstring and the self-test comment name
`deploy/demo-host/check_demo_host.sh`, the file that asserts the 302/401/200
table; the `deploy_demo_site.py` they used to name is on no ref),
sha256 `36f1ecaf…` (1,573,463 bytes, `--verify-signature` OK, re-cut as
`2026.11.23-2`); the edited copy above is 1,573,464 bytes. The BAD line prints the edited copy's own sha256 — the `sha256sum` above — so the hex moves
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
- **A `401` in the smoke's chat row is this ordering, not an expired key.** On
  a first boot the image's own `odile-agent.service` is already running when the
  installer writes `/etc/odile/key.env`. The unit carries
  `EnvironmentFile=/etc/odile/key.env`, so a gateway that exec'd before that
  file existed keeps an environment without
  `HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY` and answers
  `Provider said: HTTP 401: Invalid or expired API key` (measured on the demo
  tenant's first boot, t_bf6092d7: the file held the key, sha256
  `50daee29…`; the same value with Hermes's own body answered 200 from HQ; and
  `systemctl restart odile-agent` made the smoke pass). Before chasing NEAR,
  ask the process, not the file:
  ```sh
  $ systemctl show -p MainPID --value odile-agent
  1234
  $ sudo tr '\0' '\n' < /proc/1234/environ | grep -c HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY
  0
  ```
  `0` is the ordering; `1` means the running gateway has the key and the
  credential itself should be checked (its `sha256sum`, then the same body from
  HQ). The installer now reads that environment itself: a run that wrote the key
  file restarts the gateway and prints
  `restart odile-agent (new /etc/odile/key.env)`; a run that finds the running
  process without the key — an interrupted first run left every file in place —
  prints `restart odile-agent (the running gateway has no
  HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY)`; and a converged machine prints
  `same odile-agent active` and restarts nothing. A unit systemd still reports
  as `activating` is restarted rather than started, because `systemctl start` is
  a no-op on the process that is already coming up.
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
| 1 | the machine is the test tenant, active to 2026-12-10 | yes — `0badc0de` / `odl-fake01` / `203.0.113.87`, plan 113, SEV-SNP, measured boot, FDE requested |
| 2 | a live seed service behind `POST https://odile.fyi/seed` | yes — unit `odile-seed` active; `GET /seed` → 405 |
| 3 | an A record `<slug>.odile.fyi` | yes — `odl-fake01.odile.fyi → 203.0.113.87`, DNS-only |
| 4 | the spine is at `first_boot`, no ceremony state | yes — `tenant_state.first_boot`; `waiting_for_ceremony`, `installing` and `active` are retired (ODL-460) |
| 5 | the signed measured UKI and the pre-sealed disk | **yes, round 8 at 10:28Z, saved 14:42:53Z and booted 14:43:54Z** — UKI `5cb31808…`, disk `c1278242…`, measurement `9f01725a…`, 4-vCPU pin, published 10:29Z; the earlier builds and the defect each exposed are in `docs/evidence/m1-tenant.md` §9–§9.8 and `docs/evidence/m1-image.md` |
| 6 | a fresh setup token bound to `0badc0de` | **yes** — ledger `67de1ba9…`, live until 2026-11-13T10:23Z; its plaintext is the `odile.token=` of the sealed line |
| 7 | the owner's boot key, a passphrase, any operator key | **none** — the image's `bb.key` is a throwaway public key whose private half is shredded |
| 8 | a signed installer a stranger can obtain (independent of the image) | **yes** — on 2026-11-11 the served release `2026.11.11-5` shipped the strict-bar installer `fde7a48f…`, its `.sig`, the published key and a skills tarball carrying `odile-recipes` (§12.6); the repository's cut is `2026.11.23-2` (`36f1ecaf…` installer, 1,573,463 bytes, `e26f5fc6…` manifest, §12.6; over `2026.11.23-1`, `48d5b48a…` installer, 1,563,412 bytes, `a263d35b…` manifest — t_fe2aee5c's gate-label cut; over `2026.11.22-7`, `430d31a7…` installer, 1,563,383 bytes, `80ccb90b…` manifest — t_69a749f0's docstring cut; over `2026.11.22-6`, `5d9e9e76…` installer, 1,563,199 bytes, `7c138f5a…` manifest — t_e7e5ac73's vision-pin cut; over `2026.11.22-5`, `915d760a…` installer, 1,562,324 bytes, `a99cc9a4…` manifest — that card's pre-review cut, superseded before any merge; over `2026.11.22-4`, `f6761770…` installer, 1,557,720 bytes, `3e7c99a4…` manifest — t_509f85e4's support-grant help cut; over `2026.11.22-3`, `85282fd2…` installer, 1,557,729 bytes, `2c39128b…` manifest — t_ab95ec2a's first-person §3 cut; over `2026.11.22-2`, `87340eee…` installer, 1,557,751 bytes, `566996e7…` manifest — ODL-469's brief-email cut; over `2026.11.22-1`, `b12e9273…` installer, 1,556,716 bytes, `600bdcb7…` manifest — that card's pre-rebase cut; over `2026.11.21-6`, `b61d0713…` installer, 1,527,550 bytes, `3a5d4b89…` manifest — t_df24b754's symlinked-root cut; over `2026.11.21-5`, `9b585209…` installer, 1,527,037 bytes, `3912ce1b…` manifest — t_d55b0e47's `/verify`-unit cut on `main` `2e15b33`; over `2026.11.21-4`, `934fe8f6…`, 1,525,933 bytes, `36fcbdc1…` manifest — t_87943990's boot-key walk cut; over `2026.11.21-2`, `1536ea2f…`, 1,517,026 bytes, `2f227d34…` manifest — t_bf6092d7's gateway start-input cut; over `2026.11.21-1`, `86e87a86…`, 1,512,746 bytes), this card's symlinked-root cut on `main` `2e15b33` (whose own `2026.11.21-5` is t_d55b0e47's `/verify`-unit cut), whose `2026.11.21-4` is t_87943990's boot-key walk cut, whose own `2026.11.21-2` is t_bf6092d7's gateway start-input re-cut on the head that merges t_9574a1c8's `2026.11.21-1` help-routing cut, cut from `main` `ab3930e` whose own `2026.11.20-4` is t_e3540242's dry-run-fixture cut (`e58f31af…` installer, 1,512,539 bytes, `d1fa29c3…` manifest), cut from `main` `f54dc6e` whose own `2026.11.20-3` is t_7d51ffba's skip-link cut (`d869402e…` installer, 1,512,539 bytes, `364341fd…` manifest) that merged `main`'s `4d75a02`, whose own `2026.11.20-2` (`2159bb2a…` installer, 1,511,659 bytes, `306b8de6…` manifest — t_e23f3288's merged `/verify` grant verdict) re-cut `2026.11.20-1` (`d48f73aa…` installer, 1,503,831 bytes, `1aca6446…` manifest — t_080f8a83's installed-help re-cut) on the head that merges `2026.11.19-5` (`1d946c44…` installer, 1,503,889 bytes, `7031d4a0…` manifest), t_4fcd465f's round-4 `/verify` type re-cut on the head that merges `2026.11.19-4` (ODL-579's recovery-code cut, `6d2c3161…` installer, 1,448,387 bytes, `653b3ed5…` manifest), which re-cut `2026.11.19-3` (`00a84542…` installer, 1,436,019 bytes, `49850448…` manifest) on the head that merges `2026.11.19-1` (`e4a3554b…` installer, 1,428,062 bytes, `65c56be3…` manifest; the `/verify` page's two-URL note) with the ODL-340 follow-up's one site name and seed-derived environment — `2026.11.15-4` was re-cut at the head that merges `main`'s `-3` (ODL-522's per-machine session secret, `e0cec88a…` installer, 1,357,306 bytes, `4f0e3ef9…` manifest) and carries this card's `-2` landing (the five v3.10 access sentences on ODL-505's `-8` and t_ae833f3f's `2026.11.15-1`), then re-embedded every surface; `-6` re-embeds only the support program with t_afb081be's session signal-window fix, `-9` adds t_2efbd6e3's guest export placement, `2026.11.19-1` re-embeds the `/verify` page with the verifier's two-URL note, `2026.11.19-3` is the ODL-340 follow-up's merged head, `2026.11.19-4` is ODL-579's recovery-code cut (`2026.11.19-2` was this branch's pre-review cut, superseded before any deploy), and `2026.11.19-5` is this card's round-4 re-cut on the head that merges `-4` — and the served URL serves `2026.11.15-9` (`2cdf0801…` installer, 1,424,179 bytes, manifest `4664da84…` — measured 2026-11-16T01:44Z; still `2026.11.15-9` at 2026-11-19T11:38Z, and `2026.11.19-1` at 2026-11-19T13:01Z, again at 2026-11-19T21:18Z, still at 2026-11-20T06:56Z, and `2026.11.19-5` at 2026-11-20T16:36Z, still at 2026-11-20T20:14Z, still at 2026-11-21T09:11Z, and still at 2026-11-21T11:15Z, and still at 2026-11-21T17:03Z, and still at 2026-11-22T01:25Z, and still at 2026-11-22T06:34Z, and still at 2026-11-22T08:49Z, and still at 2026-11-22T12:38Z, and still at 2026-11-22T15:22Z, and still at 2026-11-22T20:26Z, and still at 2026-11-23T00:43Z) until a `./deploy.sh` from a `main` that carries these merges; that live cut carries the `c73dad4d…` vendor pin, and an image a build of this tree makes carries the `2026.11.23-2` pair in its `release/` directory, so the machine's `/manifest` and `/usr/local/bin/odile-install.sh` agree with each other. The live tenant's image is still `2026.11.11-8` (`4aee80c5…`, `1ed9dcad…`) until it is re-provisioned |

### 12.2 The builder's half (everything before the panel)

```sh
# 1. a fresh token, bound to the Servury server_id the image's line will carry
/usr/bin/python3 /opt/odile/seed/seed.py token rotate --tenant odl-fake01 --instance 0badc0de

# 1b. the per-tenant measured line. It must carry every field Servury's own
#     release line carries: the release installer's boot check requires exactly
#     one bb.ip4= and exits 5 without it (measured, round 4), and the disk is
#     sealed to these exact bytes. Values: the stock serial's bb.* fields for
#     this machine, a throwaway public key, a fresh random seal id, the token
#     from step 1. The panel's command line stays empty (the UKI carries this).
cat > "$OUT/boot/cmdline" <<'EOF'
root=/dev/mapper/root_crypt ro console=tty0 console=ttyS0,115200 consoleblank=0 systemd.show_status=1 ip=203.0.113.87::203.0.113.1:255.255.255.0:odl-fake01:ens18:off bb.release=2026.11.2 bb.host=odl-fake01 bb.iid=0badc0de bb.ip4=203.0.113.87/24 bb.gw4=203.0.113.1 bb.ip6=2001:0db8:0:263::1/64 bb.gw6=2001:0db8::1 bb.dns=1.1.1.1,9.9.9.9,2606:4700:4700::1111,2620:fe::fe bb.key=<base64 of the throwaway public key> seal_id=<16 hex> odile.token=<token from step 1> odile.seed_url=https://odile.fyi/seed
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

The line in `boot/cmdline` carries `bb.iid=0badc0de`, `bb.host=odl-fake01`,
the network parameters of the provider record, a throwaway `bb.key`, a random
`seal_id`, the fresh `odile.token` and `odile.seed_url=https://odile.fyi/seed`.
It is the only per-tenant input of the sealed design. Never reuse the stock
guest UUID `01494715…` for `bb.iid`: the seed token is bound to the API
`server_id` (`0badc0de`), and a first-boot POST with any other id is refused
`403 wrong_instance` (measured in ODL-456).

### 12.3 The panel step (the one human action)

Manage → **Boot image** and Manage → **Disk image**. Both take an http(s) URL
and a SHA-256; the node checks the hashes, stops the machine, snapshots and
discards the old disk, writes the image with `qemu-img convert`, and starts the
machine. The command-line field stays empty when the per-tenant line is inside
the UKI's `.cmdline` section (Servury's measured-boot guide §7). §4 of
[`docs/evidence/m1-tenant.md`](../evidence/m1-tenant.md) carries every round's
pair and names the current one (round 8, card `t_d41b2384`, saved 14:42:53Z;
§9.8 is the boot it produced). The served pair is now round 9 —
[`docs/evidence/m1-image.md`](../evidence/m1-image.md) §8, built from `main`
with the first-boot re-key — while the live tenant still boots round 8 until
the panel is given both fields.

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

**The trusted public host, and the two ends the 13 November boot paid for —
measured
2026-11-13 on `odl-fake01` (t_d95b8c13).** The re-flashed machine's first boot
ended `=== rc=1`: `systemctl start odile-access-report` failed, the installer
aborted inside `ensure_systemd_services` before its agent restart, and the
Webapp kept the listener the boot had started with, so every public Host
answered `400 Invalid Host header` (`/login`, `/api/health`,
`POST /auth/password-login`) while `/verify` stayed 200. The next boot's install
converged and then ended `=== rc=4`, which is the installer's recovery-file
confirmation refusing a machine with no TTY
(`main()`: `if not confirm_recovery(args): return 4`).

What the tree does now, and what `--self-test` asserts:

1. `ensure_systemd_services` starts `odile-agent` even when the access report
   fails — a stale `/verify` is not a dead page — and restarts it when any file
   the gateway reads at start changed (the config's public URLs, the `.env`, the
   unit, the skin), not only when a unit file did. A rewritten
   `webapp.public_url` reaches the running process only by a restart.
2. The boot half passes `--i-have-copied-it` (the first-visit page and `/verify`
   are where the user downloads `/etc/odile/recovery.txt`) and the name the
   machine was built for: the command line's `odile.public_url=`
   (`ODILE_RELEASE_PUBLIC_URL` overrides it on an operator re-run; production
   falls back to `https://<slug>.odile.fyi`), so a converged install ends
   `=== rc=0` and a machine whose owner moved its public name keeps it. It also
   passes `odile.seed_url=` through, so the installer's heartbeat receiver and
   default site name come from the environment that issued the seed (ODL-340
   follow-up).
3. The tenant config names the machine in both blocks: `webapp.public_url`,
   which Hermes v0.23.0's Webapp listener reads to set its own
   `listener_public_url` and checks every Host and Origin against, and
   `dashboard.public_url`, the dashboard surface's own name. The dashboard key
   is **not** a fallback for the Webapp: measured 2026-11-15 on the pin's own
   scratch root, removing `webapp.public_url` while keeping
   `dashboard.public_url` still answered `400 Invalid Host header`, and the
   pin's `tests/gateway/test_webapp_public_url.py` says the Webapp uses "its
   own public URL, `webapp.public_url`, and never `dashboard.public_url`".
   The fix that matters is `webapp.public_url` plus the restart in item 1;
   `--self-test` fails if the rendered template stops naming either key.

**A second environment needs no disk writes.** A tenant is
`<slug>.<the seed endpoint's host>` and its heartbeat is
`<seed origin>/heartbeat`; `odile.public_url=` in the measured command line
names a site that is neither. So a bench tenant built from this tree serves
`<slug>.test.odile.fyi` from the installer's own Caddy block, obtains its own
certificate and posts its heartbeat to the bench — with no `test-site.caddy`,
no `odile-release-setup.service.d/public-url.conf` and no `public_url` patch in
its disk (ODL-340 wrote all three; its follow-up removes the need). The boot
record is unchanged: `odile-firstboot` posts it to `odile.seed_url`, which is
already the environment that issued the seed.

The scratch proof is `installer/sim_tenant.py`: its `assert_login` speaks the
public FQDN as the Host and Origin, prints the 302/401/200 gate and a row for
the four session cookies `login_table` parses (bare names over the sim's HTTP;
the tenant's HTTPS gets the same four with the `__Host-` prefix), and also
asserts the written config carries that host in both blocks. The live proof is
§12.4.1 below.

#### 12.4.1 The live tenant's 13 November tail, and the recovery

Measured from the serial console and the tenant's own log
(`/var/log/odile-release-setup.log` behind the Webapp console,
`deploy/demo-host/tenant_console.py`):

```
=== odile-release-setup 2026-11-13T01:13:53Z · odl-fake01 · instance 0badc0de
...
same         odile-access-report enabled at boot
start        odile-access-report
RuntimeError: systemctl start odile-access-report failed (1): Job for
odile-access-report.service failed because the control process exited with
error code.
=== rc=1
```

The next run (2026-11-13T10:29:14Z) converged — `restart odile-agent (changed)`,
`listening 127.0.0.1:9130`, the ACME certificate, the smoke block all `ok` —
and ended `=== rc=4` at `write /etc/odile/recovery.txt`. `/login?next=%2F`
returned 200 from that restart on, which is why the card's title is "one boot
away from 400", not "400 today".

The recovery, measured from HQ and from the tenant's own console on
2026-11-15 (the fixed installer `2bbc70c7…` and its signature `e88576ef…`
placed at `/usr/local/bin/odile-install.sh` and
`/usr/local/lib/odile-image/odile_install.py.sig`; the setup script run as a
transient unit with `ODILE_RELEASE_FORCE=1` and
`ODILE_RELEASE_PUBLIC_URL=https://demo.odile.fyi`):

```
write        /home/tenant/.hermes/config.yaml (6 managed blocks; keys Hermes added are kept)
...
restart      odile-agent (changed)
elapsed      0m 13s
tenant: provisioned
{"changed": true, "elapsed_s": 13, "installer_sha256": "2bbc70c7…", "mode": "tenant", "ok": true, "slug": "odl-fake01"}
=== rc=0
```

and the machine's config now carries both names (`grep -A3 '^dashboard:'` and
`grep public_url ~/.hermes/config.yaml`, both `https://demo.odile.fyi`). From
HQ immediately after: `GET https://demo.odile.fyi/login?next=%2F` 200,
`GET /verify` 200, `GET /api/health` 200, `POST /auth/password-login` 200 with
four `__Host-` cookies (`{"ok":true,"next":"/"}`). The machine's own name
`odl-fake01.odile.fyi` still answers 400 by design (`demo-public.md` §Host
check): `webapp.public_url` is the demo host.

**The live machine's Hermes is not the pin.** The recovery run printed the
installer's own `warning: expected a Hermes Agent v0.23.0 install; the Webapp
gate and config schema were reviewed against it`, and `/api/health` on the
machine reports `{"version":"0.23.1","commit":"3b421578…"}`: something outside
this card updated that checkout after the image was built, while
`/etc/odile/manifest.json` still records the pin `2bd378ed`. The pin is what
`installer/sim_tenant.py` installs on a fresh root and asserts the FQDN sign-in
against; the live 200s exercise v0.23.1, and both versions read
`webapp.public_url` for the Webapp's Host check.

**The boot half on the machine is the deployer's, not the recovery run's.** The
recovery above ran the fixed script (`deploy/image/odile-release-setup.sh`)
as a transient unit. The bytes it ran are that script's `6a98879` revision,
sha256 `348794a3…`, unchanged from `763e78b`; `d43db54` later reflowed one
header comment, so the branch head hashes `6c3edc45…` — `git diff
6a98879..HEAD -- deploy/image/odile-release-setup.sh` moves comment lines and
no command. The file that unit executes is
owned by `deploy/demo-host/deploy_demo_public.py` (`RELEASE_SETUP_REMOTE`,
`:92-93`), which re-placed `main`'s copy over it. Measured on the tenant
2026-11-15T22:37:32Z:

```
$ date -u +%FT%TZ; sha256sum /usr/local/lib/odile-image/odile-release-setup.sh; stat -c "%s %y" /usr/local/lib/odile-image/odile-release-setup.sh; sudo -n systemctl cat odile-release-setup.service | grep ExecStart; sudo -n cat /var/lib/odile/release-setup.done; grep -n "i-have-copied-it\|ODILE_RELEASE_PUBLIC_URL\|public-url" /usr/local/lib/odile-image/odile-release-setup.sh
2026-11-15T22:37:32Z
036ac2b87ebace27bce2a41ba88b5b0ffb7ffdd8cd3480dd57eacf21ffdf9c81  /usr/local/lib/odile-image/odile-release-setup.sh
8438 2026-11-15 20:15:54.184000000 +0000
ExecStart=/usr/local/lib/odile-image/odile-release-setup.sh
ran 2026-11-15T20:12:42Z · odl-fake01 · instance 0badc0de · rc=0
155:        --public-url "https://$HOST.odile.fyi" --json 2>&1
```

So a *forced* re-run today would pass `main`'s slug URL and no recovery
confirmation, and would rewrite both config blocks to `odl-fake01.odile.fyi`
(400 on the demo host). The next boot is a no-op instead, because
`/var/lib/odile/release-setup.done` carries the recovery's own record. The
fixed boot half reaches the machine with the first deploy from a `main` that
carries this branch; until then, do not force the unit.

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

**The `2026.11.14-6` cut (t_781fc5cb's demo-lede re-embed).** HQ's `/verify`
demo block still said the demo was "not a tenant machine" after ODL-503 moved
the demo onto Odile's tenant machine. `web/verify.html` now quotes the v3.8b
row verbatim ("This is Odile's tenant machine: one shared workspace on our
measured AMD SEV-SNP machine. Odile holds this machine's boot key and can log
in.") and its two back links go to `https://demo.odile.fyi/login` — Savio's
2026-11-14 entry surface; `/judge` retires to a 301 on ODL-901. The page
travels inside the signed installer, so this branch re-embedded it
(`installer/embed_verify.py`; page pin `bf4dda17…`) on the head that carries
`-4`'s self-test fix (`6b42f8ef…`). The merged installer is `49f90a3c…`
(616,403 bytes). The manifest is `-4`'s document with `installer_sha256` moved
and `generated_at` set to 2026-11-14T15:12:51Z — `f6dea119…`;
`scripts/manifest.py diff` against main's document prints only
`installer_sha256` (the tool ignores `generated_at` by design), so the vendor
pin `c73dad4d…`, the skills artifact and the meter-free rows are unchanged.
`release/release.json` names installer `49f90a3c…` (616,403 bytes) and manifest
`f6dea119…`; `release/odile_install.py` is the checkout's installer and
`release/odile_install.py.sig` its detached signature; both JSON signatures
verify against `installer/odile-signing-key.pub`. The cut was made at
2026-11-14T15:12:51Z. It is committed, not deployed: measured
2026-11-14T16:40Z, `https://odile.fyi/release.json` still serves
`2026.11.14-4` (installer `6b42f8ef…`, 616,370 bytes, manifest
`65ac178c…`) and `https://odile.fyi/verify` still carries the old sentence; the next
`./deploy.sh` from a `main` that carries this merge publishes `-6`. `-5` is
carried by the ODL-505 icon branch (`legacy/wt505`), so `-6` is the next free
id.

**The `2026.11.14-7` cut (ODL-521, the image's sign-in).** This tree merges
`main`'s `-6` demo-lede cut into ODL-521's sign-in slice and cuts `-7` on the
merged head. The installer carries both: the published image's `.env` holds a
discarded scrypt hash behind `# odile-signin-placeholder: 1`, the first boot
leaves it alone, and `ensure_env(first_visit=True)` rewrites the hash on every
boot until the first-visit page records `signin_set_at`. The merged installer
is `ae2c839d…` (617,862 bytes); `installer/embed_verify.py`'s new page pin
`bf4dda17…` and the sign-in rule travel in the same bytes. The manifest is the
`-6` document with only `installer_sha256` moved — `4caad797…`; its
`generated_at` stays `2026-11-14T15:12:51Z`, the `-6` cut's value. The vendor
pin `c73dad4d…`, the skills artifact `09be3cd1…`
(851,865 bytes, the canonical `<home>/workspace` cut) and the meter-free rows
are unchanged. `release/release.json` names installer `ae2c839d…` and manifest
`4caad797…` (`822ce724…`). Both JSON signatures verify against
`installer/odile-signing-key.pub`, and `tools/recut_release.py --check` prints
`ok` for the installer, its release copy, the manifest, the release document
and the pin's hook (`949adeb2…`). The branch's pre-merge cut was
`2026.11.14-5` (installer `10a38b68…`), superseded here because `-6` landed on
`main` first. It is **committed, not deployed**: measured 2026-11-14T20:20Z,
`https://odile.fyi/release.json` serves `2026.11.14-6` (installer `49f90a3c…`,
616,403 bytes; manifest `f6dea119…`), byte-identical to
`/opt/odile/release/release.json` (mtime 2026-11-14T17:20:27Z); the next
`./deploy.sh` from a `main` that carries this merge publishes `-7`.
**The `2026.11.14-8` cut (ODL-505, ported onto the one-root tree and re-cut at the merged head).** The
repository moved to the one-root public tree while this card was in review, so
`wt/t_0017a22c` was ported onto that tree's `main`: the diff of the legacy
branch against legacy `main` `52141b9`, with `install.md` re-applied on the
scrubbed base (the branch copy carried a real demo-host address the public tree
does not) and the `docs/ops` evidence kept on disk only. The five reviewed icon
files travel inside the signed installer, base64 and each pinned by its
sha256 (`8f5f9976…`, `28bedf61…`, `623f75f5…`, `35df8e7b…`, `dd5386a9…`), and
`ensure_first_visit` writes them to `/usr/local/lib/odile/icons` (0644 root,
`same` on a re-run); the page server answers the five site paths with
`image/x-icon` or `image/png` and the year-long immutable cache, HEAD with no
body, a missing file as a 404 and no path outside its five-entry table. Caddy
splices the five exact routes only on a machine that has the page server. The
Webapp's tab is Odile's too: `ensure_webapp_brand` copies `odile-favicon.ico`
and `odile-apple-touch-icon.png` into the built bundle and repoints the shell's
three tab links, strip-then-write, so a second run prints `same`; the vendor's
`apple-touch-icon.png` is untouched, and `deploy/image/odile-webapp-brand.py`
carries all three splices (name, ODL-500 chrome, tab), so a rebuild is repaired
on the next run.

The first cut on this head was `730278bb…` at `547b4fc`; after it, `main` took
ODL-521's `-7` (`ae2c839d…`, the image's sign-in) and ODL-503-chrome
(`7ef4175`), so the branch merged `main` at `7ef4175` — 12 conflicts: the
runbook, the four signatures, the manifest and the release document by design;
the chrome add/adds resolved to `main`'s newer files; this card's tab tests
unioned into `main`'s test file — and the pair was re-cut on the merged head.
This is a fresh-root cut: both embedded pages were re-cut on this head —
`web/verify.html` is t_781fc5cb's demo lede (`bf4dda17…`) and
`web/first-visit.html` is ODL-504's page (`4d7c4960…`) — and the root is
`/var/tmp/odile-505-port/root`, provisioned by this installer with
`--first-visit --first-visit-port 9371`: run 1 exited 0 in 3m39s
(`tenant: provisioned`, installer `0c53a722…`) after `install.sh 68015 bytes,
sha256 c73dad4d… matches the pin`. Run 2 rewrote only ODL-521's placeholder
sign-in hash — `rewrite …/.env (the sign-in hash is a placeholder until first
visit)`, by design until the page records `signin_set_at` — while everything
this cut owns read `same` (the five icons, `first_visit.py`,
`first-visit.html`, `verify.html`, and `Webapp brand (…, 2 icon files)`).
After the first-visit page set the sign-in (POST 200, `signin_set_at`
2026-11-14T22:48:47Z), a further run read `tenant: same` with 0 write lines,
so the installer is idempotent on both sides of the visit.

The root holds `usr/local/lib/odile/{first_visit.py, first-visit.html,
verify.html, icons/…}` and `/usr/local/bin/odile-audit`, and the page server it
started answers the five icon paths 200 with their types, the immutable cache
and a body equal to `web/<name>`; HEAD is 200 with `Content-Length` and no
body, and `/nope.png` is 404 `no-store`. `scripts/manifest.py build --root
/var/tmp/odile-505-port/root --user odile505p --audit
…/usr/local/bin/odile-audit` produced `3a532eff…` (49,957 bytes, 48 skills, 9
packages, audit `539e205f…`); `scripts/manifest.py diff` against `main`'s `-7`
document prints exactly one changed row, `installer_sha256` (`ae2c839d…` →
`0c53a722…`; the tool ignores `generated_at` by design), so the vendor pin
`c73dad4d…`, the skills artifact and the meter-free rows are unchanged. The
installer is `0c53a722…` (1,356,516 bytes: this card's icon set, routes,
`--icons` and tab splice, ODL-521's first-visit sign-in rule and ODL-503's
chrome in the same bytes); `release/odile_install.py` is the same bytes;
`release/release.json` names installer `0c53a722…` and manifest `3a532eff…`
(`3703a3dc…`), and all four signatures verify against
`installer/odile-signing-key.pub`. The id is `-8`: `-5` was cut twice (this
card on the replaced tree at 14:49Z and odl/ODL-521 here at 15:21Z), `-6` is
t_781fc5cb's and on `main`, and `-7` is odl/ODL-521's — on `main`; it became the live cut after this
measurement (§12.1); the live cut at this cut's own measurement (2026-11-14T22:50Z) was
`2026.11.14-6` (`49f90a3c…`, 616,403 bytes, manifest `f6dea119…`), and the
next deploy publishes this cut. After the cut, `main` took t_99262fa2's type
re-cut (merged `d9ff00f`); the branch merged it at `573641b` — `BRAND.md`'s
change log was the only conflict, both 2026-11-14 rows kept, and
`deploy/demo-host/login.html` auto-merged to the sibling's type roles plus
this card's three icon links. No embedded page or tenant-bundle file is in
that diff, so the installer is unchanged at `0c53a722…` and the pair above
is the cut; the URL served `2026.11.14-7` at 2026-11-15T01:07Z. `make check`
is green at the merged head: 1,457 tests, `--self-test` PASS, secret
scan PASS (307 files, 8,665,478 bytes), claims lint 19 files / 13
phrases, brand lint 18 surfaces / 21 colours. It is committed, not deployed.

**The `2026.11.15-1` cut (t_ae833f3f, the tenant pages' type, merged onto `-8`).**
ODL-503 moved public prose onto the platform text serif but left the two tenant
pages and the skin on Cormorant: they ship byte-embedded in the signed
installer, so that card deferred them. This cut re-embeds `web/verify.html`
(page pin `00552a5c…`, 44,882 bytes) and `web/first-visit.html` (`810a596d…`,
41,485 bytes) with the roles BRAND.md §Typography names — 18 px / 1.6 prose,
16 px / 1.6 notes, Cormorant only on the wordmark and h1/h2/h3 — and re-cuts
`templates/skin.yaml` so its headings name the display face while body prose
stays the bundle's UI sans (4,978 bytes, sha `51c30f46…`, byte-identical to the
`SKIN_YAML` the self-test compares). `main` moved to `fcc87b2` (`-8`, ODL-505's
icon set and page server) while the branch was in review, so the branch merged
it first: `installer/odile_install.py` auto-merged keeping both sides, the
`install.md` conflict's three hunks were resolved by hand with both sides kept
and the numbers re-derived, and `installer/embed_verify.py` +
`installer/embed_first_visit.py` ran on the merged installer — the five icon
files, the five tenant routes, the Webapp's three tab links and
`installer/first_visit.py` (`16f820b4…`) are `-8`'s, unchanged. The merged
installer is `13863f6c…` (1,356,641 bytes); `--self-test` PASS and
`--verify-signature` OK; a one-byte edit reads `b462ab8b…` (1,356,642 bytes)
and `signature: BAD`, exit 3. `release/manifest.json` is the `-8` document
(the fresh-root build from ODL-505's install, `generated_at`
2026-11-14T22:50:20Z) with only `installer_sha256` moved — `4d0d5a2f…`:
`scripts/manifest.py diff` prints that one line, and the vendor pin
`c73dad4d…`, the skills artifact `09be3cd1…` and the meter-free rows are
unchanged. `release/release.json` names release `2026.11.15-1` at
`2026-11-15T02:50:19Z`, installer `13863f6c…` and manifest `4d0d5a2f…`
(`cb90d62e…`). `tools/recut_release.py --check` prints `ok` for the installer,
its release copy, both JSON documents and the pin's hook. The id was free at
cut time: `2026.11.15-1` was carried by this branch only and the live cut was
`2026.11.14-8`. It is committed, not deployed: measured 2026-11-15T02:52Z,
`https://odile.fyi/release.json` serves `2026.11.14-8` (installer `0c53a722…`,
1,356,516 bytes; manifest `3a532eff…`), and the next `./deploy.sh` from a
`main` that carries this merge publishes `2026.11.15-1`.

**The `2026.11.15-2` cut (ODL-474's v3.10 landing, on the ODL-505 and
t_ae833f3f head).** The v3.10 landing was built on the legacy history
(`4896ddb`, base `52141b9`); the Chief of Staff ported it as `05887d6` onto
the public `main` `55ec2c1`, and this branch merged `main`'s `7ef4175` head
(ODL-503's Webapp chrome, `-7`, the demo verifier-command port, the v3.9f
`/verify` page), then the type-role re-cut `d9ff00f`, the README rows
`ae77466`, ODL-505's `-8` (`fcc87b2`: the five icons, the five tenant routes
and the icon-aware first-visit page server) and finally t_ae833f3f's
`2026.11.15-1` branch (`d927239`: the two embedded tenant pages and the skin
re-cut onto BRAND.md's type roles; its own record is the paragraph above).
All five embed tools re-ran on the merged sources: the tenant bundle
(`odile-help` source `b726ca8c…`), the backup (`d9c4278a…`), the support
program (`0460f6ea…`), the first-visit page (`810a596d…`, 41,485 bytes) with
its five icons (`8f5f9976…`, `28bedf61…`, `623f75f5…`, `35df8e7b…`,
`dd5386a9…`), and the `/verify` page (`00552a5c…`, 44,882 bytes). The
installer is `7900e62d…` (1,413,180 bytes), up from `main`'s `-8` installer
`0c53a722…` (1,356,516 bytes): ODL-521's sign-in rule, ODL-505's icons and
routes, t_ae833f3f's page type, and this card's support block and v3.10 skill
are all in the bytes. The manifest was built from
`/var/tmp/odile-474-r6b-root`, the fresh scratch root this installer itself
made: `sim_tenant.py --reset --apply` ran 24/24, run 1 exited 0 in 4m14s,
run 2 read `tenant: same` with 0 writes, and run 1 printed `install.sh 68015
bytes, sha256 c73dad4d… matches the pin`. That root carries
`/usr/local/sbin/odile-support` (0755, 51,565 bytes, `0460f6ea…`) and
`/usr/local/bin/odile-diag` linking to it, so the A10 answer's two commands
exist on a fresh tenant; the installer's own `ensure_verify_surface` then wrote
the `/verify` page and the three programs (`c85b4c37…`, `7cff6f47…`,
`539e205f…`) on the same root before the manifest was built. The manifest is
`270f462a…` (49,957 bytes, 48 skills, audit `539e205f…`);
`scripts/manifest.py diff` against `-1`'s document prints exactly
`installer_sha256` and the `odile-help` row (`b5184e2f…` → `960f61ae…`) — the
tool ignores `generated_at`, which is 2026-11-15T03:45:00Z. The skills tarball
is `9d82653f…` (851,945 bytes). `release/release.json` (`33b7b1c8…`) names
installer `7900e62d…` and manifest `270f462a…`; `release/odile_install.py` is
the checkout's installer and `release/odile_install.py.sig` its detached
signature, and both JSON signatures verify against
`installer/odile-signing-key.pub`; `tools/recut_release.py --check` prints
`ok` for the installer, its release copy and both documents. A second cut into
`/var/tmp/odl474-r6/again2` reproduced all eight names byte for byte. It is
committed, not deployed: measured 2026-11-15T03:44Z,
`https://odile.fyi/release.json` serves the deployed `2026.11.14-8`
(installer `0c53a722…`, 1,356,516 bytes; manifest `3a532eff…`), and the next
`./deploy.sh` from a `main` that carries this merge publishes `-2`. The
release id must not be reused: `-8` is `main`'s ODL-505 cut and
`2026.11.15-1` is on this branch's ancestry (t_ae833f3f's cut, generated
2026-11-15T02:50:19Z), so `-2` was the next free id at
2026-11-15T03:45:00Z, later than every cut by `generated_at`.

**The `2026.11.15-3` cut (ODL-522, the session secret is the machine's, re-cut
at the merged head).** While this card was in review `main` took ODL-505's
`-8` and t_ae833f3f's `-1`, so the branch merged `main` `842006b` — the
installer/release conflicts resolved by taking `main`'s side whole, then
`installer/embed_first_visit.py` re-embedded the merged
`installer/first_visit.py` (`d1c761f0…`: ODL-505's five icons, t_ae833f3f's
page re-cut and this card's `SECRET_KEY` + `write_env_hash` refusal), the
installer was re-signed and `tools/recut_release.py` re-cut the chain after
demoting the conflicting id. The installer is `e0cec88a…` (1,357,306 bytes);
`release/odile_install.py` is the same bytes; `release/manifest.json` is the
merged head's fresh-root document with `installer_sha256` re-derived
(`4f0e3ef9…`); `release/release.json` is `d132f7c7…` and names the `-3` cut.
The id is `2026.11.15-3`: `-8` is ODL-505's, `-1` is `main`'s type re-cut and
`2026.11.15-2` is port/odl-474's open cut; the live cut at this cut's
measurement (2026-11-15T02:52Z) was `2026.11.14-8`. `--self-test` PASSes and
`--verify-signature` prints OK; the image build at `a651208` carries the
earlier `bd3c8963…` pair (image.md §13.9 records that image's boot), and the
per-machine secret is unchanged by this re-cut. It is committed, not deployed.

**The `2026.11.15-4` cut (ODL-474's v3.10 landing, re-cut at the ODL-522
head).** While this card was in review for the round-6 docstring fix, `main`
moved again: `ef0bc27` merged ODL-522 (`-3`, installer `e0cec88a…`) and
changed an installer input this card embeds (`installer/first_visit.py`
`d1c761f0…`, the per-machine session secret). The branch merged `main`
`ef0bc27` — the installer/release conflicts resolved by taking `main`'s side
whole — then all five embed tools re-ran on the merged sources and the
installer was re-signed and re-cut. **The `2026.11.15-4` cut, in full.** The
branch merged `main` `ef0bc27` — `installer/odile_install.py` auto-merged
keeping both sides (ODL-522's `SECRET_KEY` + `write_env_hash` refusal and this
card's support block and v3.10 skill), the installer/release conflicts resolved
by taking `main`'s side whole — then all five embed tools re-ran on the merged
sources: the tenant bundle (`odile-help` source `b726ca8c…`), the backup
(`d9c4278a…`), the support program (`0460f6ea…`), the first-visit page
(`810a596d…`, 41,485 bytes) with its five icons (`8f5f9976…`, `28bedf61…`,
`623f75f5…`, `35df8e7b…`, `dd5386a9…`), and the `/verify` page (`00552a5c…`,
44,882 bytes). The installer is `4e41122e…` (1,413,845 bytes); `--self-test`
PASS and `--verify-signature` OK; a one-byte edit reads `e52aa955…`
(1,413,846 bytes) and `signature: BAD`, exit 3. The manifest was built from
`/var/tmp/odile-474-r7-root`, the fresh scratch root this installer itself
made: `sim_tenant.py --reset --apply` ran 24/24 (run 1 exited 0 in 7m50s under
a loaded host; run 2 read `tenant: same` with 0 writes); the root carries
`/usr/local/sbin/odile-support` (0755, 51,565 bytes, `0460f6ea…`) and
`/usr/local/bin/odile-diag` linking to it. The installer's own
`ensure_verify_surface` then wrote the `/verify` page and the three programs
(`c85b4c37…`, `7cff6f47…`, `539e205f…`) on the same root before the manifest
was built. The manifest is `e933b541…` (49,957 bytes, 48 skills, audit
`539e205f…`); `scripts/manifest.py diff` against `-3`'s document prints
exactly `installer_sha256` and the `odile-help` row (`b5184e2f…` →
`960f61ae…`). The skills tarball is `9d82653f…` (851,945 bytes).
`release/release.json` (`6921e884…`) names release `2026.11.15-4` at
`2026-11-15T06:20:00Z`, installer `4e41122e…` and manifest `e933b541…`;
`tools/recut_release.py --check` prints `ok` for the installer, its release
copy and both documents; a second cut into `/var/tmp/odile-474-r7/again`
reproduced all eight names byte for byte. It is committed, not deployed:
measured 2026-11-15T06:12Z, `https://odile.fyi/release.json` serves the
deployed `2026.11.14-8` (installer `0c53a722…`, 1,356,516 bytes; manifest
`3a532eff…`), and the next `./deploy.sh` from a `main` that carries this merge
publishes `-4`. The id ledger: `-8` is ODL-505's, `2026.11.15-1` is
t_ae833f3f's type re-cut, `-2` was this branch's earlier open cut (demoted,
not reused), and `-3` is ODL-522's; `-4` is later than every cut by
`generated_at`.

**The `2026.11.15-6` cut (t_afb081be's support re-embed).** The support
program's session wrapper wrote `event=start` before it installed its
SIGTERM/SIGHUP/SIGINT handlers, so a signal that landed while the shell was
being forked killed it with no `event=end` (reproduced in a loaded `make check`
and again on a load gate on `main` at 08:36Z, where the log held only the start
line). The fix blocks the three signals before the start line, installs the
handlers, starts the shell and only then unblocks, with the child's mask
restored before exec. `installer/embed_support.py` re-embedded the program
(`f0411894…`, 53,769 bytes) and the installer was re-signed; the release was
re-cut surgically — `release/manifest.json` is `-4`'s fresh-root document
whose only move is `installer_sha256` (`4e41122e…` → `e8a33d47…`), so no root
rebuild is claimed, and the skills artifact and the `539e205f…` audit row are
unchanged. Installer `e8a33d47…` (1,416,049 bytes), manifest `92d3f747…`
(49,957 bytes), `--self-test` PASS, `--verify-signature` OK,
`tools/recut_release.py --check` prints `ok` for the installer, its release
copy and both documents; `release/release.json` (`5f15ee0f…`) names
`2026.11.15-6` at `2026-11-15T09:15:00Z`. It is committed, not deployed:
measured 2026-11-15T09:15Z, the URL served the deployed `2026.11.14-8`
(installer `0c53a722…`, 1,356,516 bytes; manifest `3a532eff…`). The id
ledger: `-8` is ODL-505's, `2026.11.15-1` is
t_ae833f3f's, `-4` is ODL-474's, `-5` is t_ab95ec2a's open cut, and `-6` is
later than every cut by `generated_at`.

**The `2026.11.15-8` cut (t_d95b8c13's boot-install fix, before the merge with
t_2efbd6e3).** The branch cut `2026.11.15-8` at `6a98879` (the boot release
install left the tenant one boot away from `400 Invalid Host header`).
The installer moved from `e8a33d47…` (1,416,049 bytes) to `3cbbaae2…`
(1,419,888 bytes): a `dashboard:` managed block whose `public_url` is the same
host as `webapp.public_url` — declared beside the key the Webapp actually
reads, with the template comment stating the measured truth that the dashboard
key is not its fallback; an `ensure_systemd_services` that starts the agent
even when the optional `odile-access-report` oneshot fails, and restarts it
when any file the gateway reads at start changed; `--self-test` assertions for
both blocks and for the five-block upgrade; a `login_table` row that asserts
the four session cookies the sign-in sets (`__Host-` over HTTPS, bare over the
simulation's HTTP); and the boot half's `--i-have-copied-it` and
`ODILE_RELEASE_PUBLIC_URL`. `release/manifest.json` is `-6`'s fresh-root
document whose only move is `installer_sha256` (`e8a33d47…` → `3cbbaae2…`), so
no root rebuild is claimed; the skills artifact (`9d82653f…`, 851,945 bytes)
and the `539e205f…` audit row are unchanged. Manifest `33f00dc5…` (49,957
bytes), `--self-test` PASS, `--verify-signature` OK,
`tools/recut_release.py --check` prints `ok` for the installer, its release
copy and both documents; `release/release.json` (`26596bee…`) names
`2026.11.15-8` at `2026-11-15T22:45:00Z`. It is committed, not deployed:
measured 2026-11-15T22:41Z, the URL served the deployed `2026.11.14-8`
(installer `0c53a722…`, 1,356,516 bytes; manifest `3a532eff…`). The id ledger:
`-7` is t_ab95ec2a's open cut, `-6` is
t_afb081be's, and `-8` is later than every cut by `generated_at`; an earlier
revision of this cut (20:00Z) was never merged or deployed. The live tenant
carries that earlier installer pair (`2bbc70c7…` / `e88576ef…`) at
`/usr/local/bin/odile-install.sh` and
`/usr/local/lib/odile-image/odile_install.py.sig`; its 2026-11-15T20:12Z
release setup ended `=== rc=0` (§12.4.1).

**The `2026.11.15-9` cut (t_2efbd6e3's guest export placement).** The
installer now carries `odilectl/offboard.py`'s `EXPORT_PROGRAM` — the bytes
`odilectl export <slug> --script` prints — and `ensure_export_program` places
them at `/usr/local/bin/odile-export` (0755 root) on every install, so a fresh
tenant has the command without the hand placement the ODL-218 drill used.
`installer/embed_export.py` embedded the program (`864604ae…`, 3,897 bytes);
`--self-test` pins it, checks it against `odilectl/offboard.py` in a checkout
and fails on a one-byte edit (a copy with one byte flipped reads `d4cd03a0…`
and prints `FAIL the embedded odile-export matches its pinned hash` plus
`FAIL this installer's shipped signature verifies`, exit 1). The scratch root
`/var/tmp/odile-218f-sim`, run by `sim_tenant.py --reset --apply` (27 checks,
run 1 exit 0 in 4m15s, run 2 `tenant: same` in 0m1s with 0 writes), carries
`/usr/local/bin/odile-export` 0755 whose SHA-256 is
`864604aefc785d3a7e0f8dd7f7c5523fdcacc538ec21c47ccea1cf74036cdf96`, equal to
`odilectl export odl-sim-203 --script | sha256sum`. The release was re-cut
surgically — `release/manifest.json` is `-6`'s fresh-root document whose only
move is `installer_sha256` (`e8a33d47…` → `2cdf0801…`), so no root rebuild is
claimed, and the skills artifact (`9d82653f…`, 851,945 bytes) and the
`539e205f…` audit row are unchanged. Installer `2cdf0801…` (1,424,179 bytes),
manifest `4664da84…` (49,957 bytes), `--self-test` PASS,
`--verify-signature` OK, `tools/recut_release.py --check` prints `ok` for the
installer, its release copy and both documents; `release/release.json`
(`5bee6fd3…`) names `2026.11.15-9` at `2026-11-15T23:59:00Z` (re-cut from
22:25Z after t_d95b8c13 re-cut `-8` at 22:45Z). It was committed, not
deployed at its cut; the URL has since served it — measured 2026-11-16T01:44Z,
`2026.11.15-9` (`2cdf0801…`, 1,424,179 bytes; manifest `4664da84…`). The id
ledger: `-7` is t_ab95ec2a's open cut
(10:45Z), `-8` is t_d95b8c13's (re-cut at 22:45Z, `6a98879`), and `-9` is
later than every cut by `generated_at` as of its 2026-11-15T23:59:00Z cut.

**The `2026.11.16-1` cut (the merged head: t_d95b8c13 and t_2efbd6e3).** The
merge of `main`'s `-9` (`2cdf0801…`) with this card's branch produced one
installer, `37860cc9…` (1,428,018 bytes): `-9`'s guest export placement and
this card's dashboard/restart/login-table changes are both present, and
`--self-test` exercises both. `release/manifest.json` is `-9`'s fresh-root
document whose only move is `installer_sha256` (`2cdf0801…` → `37860cc9…`), so
no root rebuild is claimed; the skills artifact (`9d82653f…`, 851,945 bytes)
and the `539e205f…` audit row are unchanged. Manifest `6bc8f558…` (49,957
bytes), `--self-test` PASS, `--verify-signature` OK,
`tools/recut_release.py --check` prints `ok` for the installer, its release
copy and both documents; `release/release.json` (`33b4e772…`) names
`2026.11.16-1` at `2026-11-16T01:40:00Z`. It is committed, not deployed:
measured 2026-11-16T01:44Z the URL serves `2026.11.15-9` (`2cdf0801…`,
1,424,179 bytes; manifest `4664da84…`, release `5bee6fd3…`). The id ledger:
`-7` is t_ab95ec2a's open cut (10:45Z), `-8` is this card's pre-merge cut
(22:45Z, superseded by this merged head), `-9` is t_2efbd6e3's (23:59Z), and
`2026.11.16-1` is later than every cut by `generated_at`.

**The `2026.11.19-1` cut (ODL-906-F3 follow-up, `t_3cd62ef9`: the `/verify`
page's fetch count).** `web/verify.html` (and its byte-identical public copy,
`web/verify-public.html`) replaced "It fetches one URL: `/attestation`" with
"It fetches two URLs: `/attestation`, and that report's own VCEK from AMD's
KDS" — the sentence the shipped verifier has outgrown since the demo chip's
vendored VCEK (`tests/fixtures/amd/vcek.pem`) was deleted for its silicon id.
`installer/embed_verify.py` re-embedded the page, `tools/recut_release.py`
re-derived and re-signed the pair (`--check` prints `ok` for the installer, its
release copy and both documents), `--self-test` prints `self-test: PASS`, and
`--verify-signature` prints `signature: OK`: installer `e4a3554b…` (1,428,062
bytes), manifest `65c56be3…` (49,957 bytes; its only move against `-1` is
`installer_sha256`), release.json `e22b8ef6…` naming `2026.11.19-1` at
`2026-11-19T05:00:00Z`. No root rebuild is claimed: the manifest is
`2026.11.16-1`'s fresh-root document and the skills artifact (`9d82653f…`,
851,945 bytes) and the `539e205f…` audit row are unchanged. The verifier
bundle itself did not move (20,560 bytes, sha256 `815dbc26…08d4c`): the page
is not one of its members. It is committed, not deployed: measured
2026-11-19T04:37Z the URL still serves `2026.11.15-9` (`2cdf0801…`,
1,424,179 bytes; release.json `5bee6fd3…`). The id ledger: the newest cut by
`generated_at` before this one is `2026.11.18-1` (t_4fcd465f,
`2026-11-18T18:30:00Z`, an unmerged branch), and this tree's base is
`2026.11.16-1` (`2026-11-16T01:40:00Z`).

**The `2026.11.19-2` cut (the ODL-340 follow-up merged with `-1`).** The
merge of `2026.11.19-1` (`e4a3554b…`, the `/verify` page's two-URL note) with
the ODL-340 follow-up produced one installer, `36babff5…` (1,433,661 bytes):
`-1`'s re-embedded page and this card's one site name
(`render_caddy_site`, `ensure_caddy_site`, `wait_for_dns`, `wait_for_acme` and
`certificate_fingerprint` all read `layout.fqdn`, so `--public-url` names the
Caddy site, the DNS and ACME waits and the certificate the recovery file pins)
and its seed-derived environment (`<slug>.<seed endpoint's host>` for a tenant,
`<seed origin>/heartbeat` for the nightly unit; production keeps
`<slug>.odile.fyi` and `https://odile.fyi/heartbeat`) are both present, and
`--self-test` exercises both. The boot half
(`deploy/image/odile-release-setup.sh`) passes the measured
`odile.public_url=` as `--public-url` (`ODILE_RELEASE_PUBLIC_URL` still wins on
an operator re-run) and `odile.seed_url=` through, so a second environment
needs no systemd drop-in. `release/manifest.json` is `2026.11.16-1`'s
fresh-root document whose only moves are `installer_sha256`, so no root
rebuild is claimed; the skills artifact (`9d82653f…`, 851,945 bytes) and the
`539e205f…` audit row are unchanged. Manifest `be75fb66…` (49,957 bytes),
`--self-test` PASS, `--verify-signature` OK,
`tools/recut_release.py --check` prints `ok` for the installer, its release
copy and both documents; `release/release.json` (`6f2c8a95…`) names
`2026.11.19-2` at `2026-11-19T11:40:00Z`. It is committed, not deployed:
measured 2026-11-19T11:38Z the URL serves `2026.11.15-9` (`2cdf0801…`,
1,424,179 bytes; manifest `4664da84…`). The id ledger: `-9` is t_2efbd6e3's
(23:59Z), `2026.11.16-1` is t_d95b8c13/t_2efbd6e3's merged-head cut (01:40Z),
`2026.11.19-1` is t_3cd62ef9's `/verify` cut (05:00Z), and `2026.11.19-2` is
later than every cut by `generated_at`.

**The `2026.11.19-3` cut (the ODL-340 follow-up, review round 1's two fixes).**
`-2` above was cut on the pre-review tree at `2026-11-19T11:40:00Z` and
superseded before any deploy. Round 1 (artifact lens) found two defects in the
shipped surface: `docs/runbooks/image.md` §2 quoted `-2`'s predecessor
(`fa51daa1…`/`f73b5d4e…`, a build no reader can obtain) as "this tree's
`release/`", and `dry_run` resolved the layout before it read the recovery
kit, so the documented machine-free command `--dry-run --recovery-kit <kit>`
printed the literal `<slug>.odile.fyi` for the new site line, the Webapp's
`public_url` and the DNS/ACME wait. `dry_run` now loads the kit first and
calls `resolve_layout(args, slug=…)`, and `--self-test` pins both
environments: `site odl-dry.odile.fyi` on a production seed,
`site odl-dry.test.odile.fyi` and `heartbeat https://test.odile.fyi/heartbeat`
on the bench's, `--public-url` then `--slug` still winning. The installer
therefore moved: sha256 `00a84542…` (1,436,019 bytes), manifest `49850448…`
(49,957 bytes; its only move is `installer_sha256`), `release/release.json`
(`8cc6ae53…`) naming `2026.11.19-3` at `2026-11-19T13:10:00Z`. No root rebuild
is claimed: the skills artifact (`9d82653f…`, 851,945 bytes) and the
`539e205f…` audit row are unchanged, `--self-test` prints `self-test: PASS`,
`--verify-signature` prints `signature: OK`, and
`tools/recut_release.py --check` prints `ok` for the installer, its release
copy and both documents. `deploy/image/odile-firstboot.py`'s docstring now
also states what the installer derives when `odile.public_url=` is absent. It
is committed, not deployed: measured 2026-11-19T13:01Z the URL serves
`2026.11.19-1` (`e4a3554b…`, 1,428,062 bytes; manifest `65c56be3…`). The id
ledger: `-1` is t_3cd62ef9's `/verify` cut (05:00Z), `-2` is this card's
pre-review cut (11:40Z, never deployed), `2026.11.19-3` is later than every
cut by `generated_at`, and the bench image of §2 of `image.md` boots this cut.

**The `2026.11.19-4` cut (ODL-579, the machine's recovery-code card).** This
card re-embeds `installer/first_visit.py` and `web/first-visit.html` — the
service and page that make the recovery code single-use at both use sites and
rotate it on every use, mint the one-time `?d=` download token, and rotate
`HERMES_DASHBOARD_BASIC_AUTH_SECRET` on every sign-in reset — so the installer
moves from `00a84542…` (1,436,019 bytes) to `6d2c3161…` (1,448,387 bytes). The
manifest is the `-3` document with only `installer_sha256` moved:
`653b3ed5…` (49,957 bytes; `diff` against the committed `-3` document prints
that one line). `release/release.json` (`e3e7a2fc…`) names the pair at
`2026-11-19T15:45:00Z`. No root rebuild is claimed: the skills artifact
(`9d82653f…`, 851,945 bytes) and the `539e205f…` audit row are unchanged,
`--self-test` prints `self-test: PASS`, `--verify-signature` prints
`signature: OK (sha256 6d2c3161…)`, and `tools/recut_release.py --check`
prints `ok` for the installer, its release copy and both documents (its one
`warn` is the pre-existing `backup` row the manifest predates). It is
committed, not deployed: measured 2026-11-19T19:37Z the URL serves
`2026.11.19-1` (`e4a3554b…`, 1,428,062 bytes; manifest `65c56be3…`). The id
ledger: `-1` is t_3cd62ef9's `/verify` cut (05:00Z), `-2` is this card's
pre-review cut (11:40Z, never deployed), `-3` is the ODL-340 follow-up's
(13:10Z), and `2026.11.19-4` is later than every cut by `generated_at`
(15:45Z). §2 of `image.md` remains the `-3` bench measurement; a build of
this tree carries the `-4` pair.

**The `2026.11.19-5` cut (t_4fcd465f's round-4 `/verify` on the merged head).**
The head merges `2026.11.19-4` (ODL-579's recovery-code cut, `6d2c3161…`), so
the installer carries that cut's first-visit service and page and this card's
`/verify` surface. `installer/embed_first_visit.py` and
`installer/embed_verify.py` re-embedded both; `tools/recut_release.py`
re-derived and re-signed the pair (`--check` prints `ok` for the installer, its
release copy, the `backup` row and both documents), `--self-test` prints
`self-test: PASS`, and `--verify-signature` prints `signature: OK
(sha256 1d946c44…)`. The installer is `1d946c44…` (1,503,889 bytes) and the
manifest `7031d4a0…` (50,044 bytes); against the committed `-4` document its
diff is two lines — `installer_sha256` and the `backup_sha256` row the `-4`
document predated, which the cut now writes, so `--check`'s old `warn` for that
row is `ok` here. `release/release.json` (`fa5b11fc…`) names `2026.11.19-5` at
`2026-11-19T21:20:00Z`. No root rebuild is claimed: the skills artifact
(`9d82653f…`, 851,945 bytes) and the `539e205f…` audit row are unchanged. What
moved since `-4` is the tenant page's type roles: `templates/verify.html`
declares `--text` and `--serif` in `:root`, sets body prose at 18 px/1.6 on the
text face and notes and the foot at 16 px/1.6, and keeps Cormorant Garamond for
`.brand`, `h1` and `h2`; `tests/test_verify.py` pins the roles. It is
committed, not deployed: measured 2026-11-19T21:18Z the URL serves
`2026.11.19-1` (`e4a3554b…`, 1,428,062 bytes; manifest `65c56be3…`). The id
ledger: `-1` is t_3cd62ef9's `/verify` cut (05:00Z), `2026.11.19-2` was this
branch's pre-review cut (11:40Z, never deployed), `-3` is the ODL-340
follow-up's (13:10Z), `-4` is ODL-579's recovery-code cut (15:45Z), and
`2026.11.19-5` is later than every cut by `generated_at` (21:20Z). This
branch's round-3 cut also used the id `2026.11.19-4` (installer `6959f3fe…`,
1,491,364 bytes, at 20:16Z) before `main` took that id; it was superseded by
the merge and never deployed. §2 of `image.md` remains the `-3` bench
measurement; a build of this tree carries the `-5` pair.

**The `2026.11.20-1` cut (t_080f8a83, the installed help's bill answer).**
`skills/odile-help/SKILL.md` moment 4 ("A bill or a charge") answered a bill
question with the v5 line — "$49 a month, founding price, month-to-month. The
$12 inference allowance is included and hard-capped" — long after `t_02310cbf`
(`ddcde16`) had re-cut `web/claim.html`, `templates/emails/support.md` and
`docs/FAQ.md` moment 4. This card re-cuts the skill to the same shape: "The
private alpha has no price: Odile pays for the machine and the inference.
Stripe holds the card; we hold the customer id, the subscription id and whether
it is paid. Reply to help@odile.fyi for anything on the charge; it is read by a
person, and the target is an answer within 24 hours." The held `$12` allowance
leaves with the price (a figure, not a plan, to state while it is free), and
the "Never promise a refund, a credit or a discount…" instruction stays under
the moment. The skill travels inside the installer (`TENANT_SKILLS`) **and** in
the release's skills artifact, so both halves moved: `installer/embed_tenant.py`
re-embedded it (`09c301f2…`, 6,071 bytes) and the cut rebuilt `skills.tar.gz`
(`ccff7327b9f5…`, 851,913 bytes) from a fresh scratch root the signed installer
made. The installer moved from `1d946c44…` (1,503,889 bytes) to `d48f73aa…`
(1,503,831 bytes). The manifest is a fresh-root build from that same root
(`scripts/manifest.py build --root /var/tmp/odile-080f8a83-cand --user
odile080f --audit scripts/audit.py` at `2026-11-20T07:01:51Z`), not a surgical
edit, so the `odile-help` skills row moves with the artifact it names
(`960f61ae…` → `928bbff0…`); against the committed `-5` document the diff is
two lines — `installer_sha256` and that row — and `manifest.json` is
`1aca6446…` (50,044 bytes). `release/release.json` (`c3bdefaf…`) names
`2026.11.20-1` at `2026-11-20T07:01:51Z`. `--self-test` prints `self-test:
PASS` on both the checkout's installer and `release/odile_install.py`,
`--verify-signature` prints `signature: OK (sha256 d48f73aa…)`, and
`tools/recut_release.py --check` prints `ok` for the installer, its release
copy, the backup row and both documents. The fresh root ran 29/29 with run 2
reading `tenant: same` and no writes; its installed
`home/odile080f/.hermes/skills/odile-help/SKILL.md` is the pinned `09c301f2…`,
and the tenant's agent answered "what does Odile cost per month?" with the
no-price reply, no figure in it. It is committed, not deployed: measured
2026-11-20T06:56Z the URL serves `2026.11.19-1` (`e4a3554b…`, 1,428,062 bytes;
manifest `65c56be3…`). The id ledger: `-1` is t_3cd62ef9's `/verify` cut
(05:00Z), `-2` was t_4fcd465f's pre-review cut (11:40Z, never deployed), `-3` is
the ODL-340 follow-up's (13:10Z), `-4` is ODL-579's recovery-code cut (15:45Z),
`-5` is t_4fcd465f's round-4 `/verify` cut (21:20Z), and `2026.11.20-1` is
later than every cut before it by `generated_at` (07:01Z). §2 of `image.md`
remains the `-3` bench measurement; a build of this tree carries the `-3` pair
of 2026-11-20, cut below.

**The `2026.11.20-2` cut (t_e23f3288's round 7, the merged `/verify` grant cut).**
This branch merges `main` `88b5751`; the merge's only product surfaces are the
scoped tenant row and the reporter's grant verdict. `installer/embed_first_visit.py`
and `installer/embed_verify.py` re-embedded from the merged tree (`first_visit.py`
`21c84e64…`, `verify.py` `70d8f469…`, `verify.html` `543f42ea…`,
`verify_and_unlock.sh` `332eccdf…`), `tools/recut_release.py` re-signed the pair,
and the installer is `2159bb2a…` (1,511,659 bytes) with `manifest.json`
`306b8de6…` (50,044 bytes). `release/release.json` (`15cb66b4…`) names
`2026.11.20-2` at `2026-11-20T16:34:05Z`. The id is `-2` because both this
branch and `main` had cut a `2026.11.20-1` (this branch's at 00:45Z, `main`'s at
07:01Z, `d48f73aa…`); one merged tree carries one cut id per artifact. The
skills artifact is unchanged (`ccff7327…`, 851,913 bytes) and the `odile-help`
row still names it. `--self-test` prints `self-test: PASS` on both the
checkout's installer and `release/odile_install.py`; `--verify-signature` prints
`signature: OK (sha256 2159bb2a…)` on both; a one-byte edit prints
`signature: BAD (sha256 f8411ae1…)` and exits 3; and
`tools/recut_release.py --check` prints ten `ok` lines. It is committed, not
deployed: measured 2026-11-20T16:36Z the URL serves `2026.11.19-5`
(`1d946c44…`, 1,503,889 bytes; manifest `7031d4a0…`).

**The `2026.11.20-3` cut (this card: the skip link on the two embedded pages).**
This branch merges `main` `4d75a02`, whose `2026.11.20-2` is t_e23f3288's
`/verify` grant cut above. The merge's only product surfaces are the three skip
hunks that `main`'s pages had not carried: `web/first-visit.html` and the
tenant `/verify` page (`templates/verify.html`, rendered by `verify/verify.py`)
now open with `<a class="skip" href="#main">Skip to content</a>` before a
`<main id="main">`. The link is at `left:-9999px` until `:focus-visible`, when
it paints the sheet's 2 px `--ink` ring at a 3 px offset and follows `main`'s
24 px inset at 1440 and 18 px at 390. BRAND.md §Accessibility tokens, Keyboard
row, requires it: these pages carry more than one landmark (`main` plus the
named section; `main` plus four named regions on the tenant `/verify`), and the
first Tab stop was the `Odile` wordmark.
`installer/embed_first_visit.py` and `installer/embed_verify.py` re-embedded
from the merged tree (`first_visit.py` `21c84e64…`, `verify.py` `70d8f469…`,
`verify.html` `548bc270…`, 42,968 bytes, the first-visit page `6758b591…`,
44,512 bytes, `verify_and_unlock.sh` `332eccdf…`); `tools/recut_release.py`
re-derived and re-signed the pair, and the installer is `d869402e…` (1,512,539
bytes) with `manifest.json` `364341fd…` (50,044 bytes).
`release/release.json` (`a09c7836…`) names `2026.11.20-3` at
`2026-11-20T20:12:55Z`. The id is `-3` because `main`'s `-2` cut (16:34:05Z)
landed after this branch's earlier one; one merged tree carries one cut id per
artifact. The skills artifact is unchanged (`ccff7327…`, 851,913 bytes) and the
`odile-help` row still names it; the handoff audit is still `539e205f…`.
`--self-test` prints `self-test: PASS` on both the checkout's installer and
`release/odile_install.py`; `--verify-signature` prints `signature: OK (sha256
d869402e…)` on both; a one-byte edit prints `signature: BAD (sha256
0aa97d28…)` and exits 3; and `tools/recut_release.py --check` prints ten `ok`
lines. The pages keep their one `h1` and their landmark set: the skip link is
an anchor outside `main`, so nothing is added or removed, and
`tests/test_first_visit.py` and `tests/test_verify.py` walk the rendered
document order (first tab stop, `href` equals the page's own `main` id) and pin
the rule's declarations. `web/verify-public.html`, the copy HQ serves at
`/verify`, stays byte-identical to `web/verify.html` (`db1975ee…`, 45,366
bytes). It is committed, not deployed: measured 2026-11-20T20:14Z the URL
serves `2026.11.19-5` (`1d946c44…`, 1,503,889 bytes; manifest `7031d4a0…`).
The id ledger: `-1` is t_080f8a83's installed-help cut (07:01Z), `-2` is
t_e23f3288's `/verify` grant cut (16:34Z), and `2026.11.20-3` is later than
every cut before it by `generated_at` (20:12Z). A build of this tree carries
the `-3` pair of 2026-11-20.

**The `2026.11.20-4` cut (t_e3540242: the frozen pair's dry-run fixture).**
This branch is cut from `main` `f54dc6e`, whose `-3` is the skip-link cut
above. The installer's own `--self-test` writes a dry-run kit before it exercises
`--dry-run --recovery-kit <kit>`, and that fixture named the live Servury box id
(`json.dumps({"slug": "odl-dry", "instance_id": …})`, added by `4fd9731`,
ODL-340's round 2). `installer/` and `release/` are `FROZEN` in
`tools/build_public_tree.py` — the installer pins a SHA-256 for every file it
embeds and `release/` carries detached signatures — so the public build never
scrubbed it, and the forbid list's `servury-box-id` class found it in a built
tree at both frozen paths (`forbidden-value`, line 15353 at `e399191` and
15538 at this head, digest `3e5275784e44`). The fixture now carries the scrub's
own same-length stand-in, `deadbeef`, so the frozen bytes are clean at the
source: `make leak FORBID_FILE=docs/ops/leak-forbid.txt` in the HQ checkout (a
branch worktree has no `docs/ops` copy, so there a bare `make leak` prints
`forbid list: absent` and runs no forbid class) finds nothing there, and a built
tree scanned with the same `--forbid-file` PASSes with the two `forbidden-value`
rows for these two paths deleted from its own allowlist as well, so nothing is
waived.
`release/odile_install.py` is the checkout's byte-identical copy (`cmp` exit 0)
at `e58f31af…` (1,512,539 bytes — the stand-in is the same length, so the size
is `-3`'s), and
`tools/recut_release.py` re-derived and re-signed the pair: `manifest.json` is
`d1fa29c3…` (50,044 bytes) and against the committed `-3` document its diff is
two lines, `installer_sha256` and `generated_at`. `release/release.json`
(`783deb94…`) names `2026.11.20-4` at `2026-11-20T23:12:39Z`. No root rebuild
is claimed: the skills artifact (`ccff7327…`, 851,913 bytes), the `odile-help`
row, the `539e205f…` handoff audit and the `d9c4278a…` backup row are
unchanged. `--self-test` prints `self-test: PASS` on both the checkout's
installer and `release/odile_install.py`; `--verify-signature` prints
`signature: OK (sha256 e58f31af…)` on both; a one-byte edit prints
`signature: BAD (sha256 d05974c4…)` and exits 3; and
`tools/recut_release.py --check` prints ten `ok` lines. The two allowlist rows
for these paths stay, reworded to what they now cover: `origin/main`'s history
still carries the blob that named the id (`4fd9731`), so CI's `history-secrets`
job needs the row, while the tree no longer does. The id ledger: `-1` is
t_080f8a83's installed-help cut (07:01Z), `-2` is t_e23f3288's `/verify` grant
cut (16:34Z), `-3` is t_7d51ffba's skip-link cut (20:12Z), and `2026.11.20-4`
is later than every cut before it by `generated_at` (23:12Z). It is committed,
not deployed: measured 2026-11-20T23:16:57Z the URL serves `2026.11.19-5`
(`1d946c44…`, 1,503,889 bytes; manifest `7031d4a0…`). §2 of `image.md` remains
the `-3` bench measurement; a build of this tree carries the `-4` pair.

**The `2026.11.21-1` cut (t_9574a1c8: the installed help is selected for a
money question).** This branch is cut on `main`'s `ab3930e` — whose `-4` is
t_e3540242's dry-run-fixture cut above — and rebased onto `dbbebfb`, its
merge-base with `main` (the merge of `odl/public-repo-drift`). Round 2 named a
cost and a bill in the selector; a
fresh-root ask "how much is the subscription?" still answered with the Nous
Portal tiers and four `$` figures, because neither selector named a subscription
and the bundled `hermes-agent` skill won the word. This round's
`installer/embed_tenant.py`
re-embeds `skills/odile-help/SKILL.md` at `ace83094…` (6,278 bytes), so the
installer moves from `e58f31af…` (1,512,539 bytes) to `86e87a86…` (1,512,746
bytes). No figure is re-quoted and `docs/CLAIMS.md` gains no row — a
money/support fact. The change is a selector — the frontmatter `description`
the system-prompt skill index shows is now `Odile price, bill, subscription?
odile.fyi/faq · odile-diag.` at 60 characters, and the load line names what
Odile or a subscription costs and a charged card; moment 4 is headed "A bill, a
charge or a subscription" — plus the round-2 pin on moment 4's words: "Answer
in these words; do not paraphrase:" above the unchanged blockquote, "Never
quote a figure: a number that is not in the words above is one I invented."
below it, so a paraphrase cannot drop "we hold the customer id, the subscription
id and whether it is paid". The moves from `ab3930e` to `dbbebfb` touch 49
files — most of them the operating-record documents `odl/public-repo-drift`
withdrew, plus the HQ merge tool's own test module removed and
`tests/test_public_tree_shape.py` added, the commit-message gate,
`deploy/demo-host/` and `tests/test_seed.py` (+32) — and none is
`installer/odile_install.py` or under `templates/`, `web/`, `scripts/`,
`support/`, `odilectl/` or `skills/`, so the only embedded byte this cut moves
is its own skill and the pair was cut once. The manifest and the skills
artifact are a fresh-root cut, not a surgical edit: `scripts/manifest.py
build --root /var/tmp/odile-t9574a1c8-sim --user odilet9574a --audit
scripts/audit.py --generated-at 2026-11-21T11:13:47Z` from the root this
installer made (`installer/sim_tenant.py --reset --apply`, 29/29; run 2 read
`tenant: same` with 0 writes), so `manifest.json` is `95aca3ac…` (50,044
bytes) and against the `-4` document its diff is two lines —
`installer_sha256` and the `odile-help` skills row (`928bbff0…` →
`3800d71f…`). A second fresh root (`odilet9574b`) of the same installer
reproduced the manifest byte for byte (`cmp` exit 0), so the cut is a function
of the signed pair, not of one root. `skills.tar.gz` is `a813f1de…` (852,004
bytes) and `release/release.json` (`705f9e6b…`, 799 bytes) names
`2026.11.21-1` at `2026-11-21T11:13:47Z`. On the first root the installed help
answered all three unprimed asks — "what does Odile cost per month?", "why was
I charged on my card?", "how much is the subscription?" — one session each, in
the tenant's own chat, with the moment's words: no `$`, no figure, no connector
punt; the third answer (the subscription one) then carried the model's own tail
("Long version: https://odile.fyi/faq"), so that tail is the model's, not the
skill's. That chat is the surface this change carries, because its full-toolset
prompt's skill index carries the row
`odile-help: Odile price, bill, subscription? odile.fyi/faq · odile-diag.` The
prompt's hash and length are not quoted here, because they belong to the
machine, not to the cut: the prompt embeds the home, cwd and scratch paths and
carries the `AGENTS.md` project block only when the chat starts in the home
directory. Measured on this cut's own root (`/var/tmp/odile-t9574a1c8-r4`, user
`odilet9574r4`), the full-toolset prompt was 15,506 characters and the two
`-t terminal` prompts 8,631 and 8,621; the verifier's round-3 probe root gave
13,544 and 8,643. What reproduces on any root is the mechanism: `select prompt
from system_prompts` finds the index row in the full-toolset prompt and no
skills index at all in the `-t terminal` prompt, so the frontmatter description
cannot be read in the smoke-flag mode and `odile-help` is never selected
there. Measured on a fresh root at this cut, one session each: "what does
Odile cost per month?" quoted the FAQ's retired `$49` figure, the `$33.66`
per-tenant cost and the `$12`/`$10` allowances; "why was I charged on my card?"
punted to a missing bank/email connection; "how much is the subscription?"
returned the bundled `hermes-agent` skill's Nous Portal tiers (`$0`, `$20`,
`$100`, `$200`). The earlier run on the cut's own root — the one that returned
the moment's words 3/3 — carried two tails of its own, "This machine is up and
answering…" and the first-run greeting ("What should I call you?"), which
belong to this mode, not to the tenant chat, so the mode is not a stable
surface for the selector.
No `AGENTS.md` router line is added for it: it would re-embed and re-sign the
installer to make a diagnostic prompt carry a selector, so this round is
documentation only and none of the four release bytes above moves.
`--self-test` prints `self-test: PASS` on
both the checkout's installer and `release/odile_install.py`;
`--verify-signature` prints `signature: OK (sha256 86e87a86…)` on both; a
one-byte edit prints `signature: BAD (sha256 8d3eb381…)` and exits 3; and
`tools/recut_release.py --check` prints ten `ok` lines. It is committed, not
deployed: measured 2026-11-21T11:15:01Z the URL serves `2026.11.19-5`
(`1d946c44…`, 1,503,889 bytes; manifest `7031d4a0…`). The id ledger: `-1` is
t_080f8a83's installed-help cut (07:01Z), `-2` is t_e23f3288's `/verify` grant
cut (16:34Z), `-3` is t_7d51ffba's skip-link cut (20:12Z), `-4` is
t_e3540242's dry-run-fixture cut (23:12Z), and `2026.11.21-1` is later than
every cut before it by `generated_at` (2026-11-21T11:13Z). §2 of `image.md`
remains the `-3` bench measurement; a build of this tree carries the
`2026.11.21-1` pair.

**The `2026.11.21-6` cut (t_df24b754: a private root that is itself a
symlink).** This branch merges `main`'s `2e15b33` (t_d55b0e47's `/verify`-unit
cut, `2026.11.21-5`; through its parent merge `4d88408` the tree also carries
`2026.11.21-4`, t_87943990's boot-key walk cut), so both fixes are in one
installer (its `VERIFY_UNIT_TEMPLATE` carries `ReadWritePaths=/run`, which
`--self-test` asserts, and the embedded audit below). Its product surface is the
boot-key row's
candidate set, not a new row. `scripts/audit.py` now runs `find(1)` with `-H`,
so a private root that is itself a symlink to a directory is walked, exactly as
the Python fallback (`os.scandir`) always walked it: without the flag `find`
printed nothing for such a root, which is a well-formed empty stream, and the
row read PASS over a planted key (measured on t_87943990, whose review filed
this card). Odile's installer creates all four roots as real directories
(`ensure_dirs`), so no shipped or demo shape was affected, and on a real-dir tree
the flag costs nothing: on a fixed 20,000-file tree each shape read the same
20,000 files and 20,000 heads, 0.354–0.395 s without `-H` and 0.369–0.423 s with
it (four draws each, interleaved). On the demo host (2 vCPU, SEV-SNP, through
the tenant console) this cut's `scripts/audit.py` (`6d3f7be0…`, 56,113 bytes)
over the machine's own roots answered in 1,936 / 1,967 / 1,989 ms (rc 0, the same
10,103-byte report as the installed `fd66f284…` build's 1,943 / 1,966 / 1,972 ms,
byte-identical apart from the report's `read from your machine` timestamp line),
while `/verify` on loopback `http://127.0.0.1:8098/verify` answered 1,901 /
1,964 / 2,008 s (51,156 bytes, `Cache-Control: no-store`) — inside the ~2 s the
`-4` cut achieved. The row's proof is unchanged — the same four roots, the same
200..65536-byte window, the same `.pub`/`authorized_keys*`/`known_hosts*`
exclusions, the same 48-byte `PRIVATE KEY` marker and the same
`ssh-keygen -y`/`.pub`-sibling identification — and the Python walk remains the
fallback when find cannot answer. A nested symlink to a directory is followed by
neither path, and a nested symlink to a file is a candidate on both, as the
docstring now says. `tests.test_audit`'s `WalkCostTest` pins it: a symlinked
root holding the planted key FAILs `boot key` on the fast path with
`_scan_candidates` not returning `None` (60 tests, `OK`).
`tools/recut_release.py` re-derived and re-signed the pair over `main`'s
`2026.11.21-5`: the installer is `b61d0713…` (1,527,550 bytes) and
`manifest.json` `3a5d4b89…` (50,044 bytes); against `-5`'s document the `diff`
is two lines, `audit_sha256` and `installer_sha256`, so this is a surgical
re-cut and not a root rebuild — the skills artifact (`a813f1de…`, 852,004
bytes), the `odile-help` row, the `d9c4278a…` backup row and t_d55b0e47's
`ReadWritePaths=/run` unit are `main`'s. `release/release.json` (`27304e06…`,
1,924 bytes) names `2026.11.21-6` at `2026-11-22T01:24:27Z`. `--self-test`
prints `self-test: PASS` on the checkout's installer and on
`release/odile_install.py`; `--verify-signature` prints
`signature: OK (sha256 b61d0713…)` on both; a one-byte edit prints
`signature: BAD (sha256 36092d18…)` and exits 3; and
`tools/recut_release.py --check` prints eleven `ok` lines. Measured
2026-11-22T01:25Z the URL serves `2026.11.19-5` (`1d946c44…`, 1,503,889 bytes;
manifest `7031d4a0…`), and a deploy from a `main` that carries this cut is what
moves it. The id ledger: `-5` is t_d55b0e47's `/verify`-unit cut (23:25Z) and
`-6` is later than every cut before it by `generated_at` (01:24Z).

**The `2026.11.22-6` cut (t_e7e5ac73 / ODL-341-F1: a tenant's image is described
by a vision model in the Private TEE tier, chosen for its answer).** The head
merges `main`'s `cd6734f`. This card's first cut took `-4`'s id while the card
waited for its gate and was re-cut as `-5` once `main` took `-4`; review then
found the tier note false — the pin and the block are unchanged, only the prose
moved. The config template gains a seventh managed block,
`auxiliary.vision.model` = `Qwen/Qwen3-VL-30B-A3B-Instruct`, and `MANAGED_KEYS`
owns `auxiliary`, so a re-run writes the pin and appends the block to a config
this program wrote before it existed. The model is **not** the only vision model
in the tier: `https://near.ai/pricing`'s Privacy column lists Qwen3.8-27B and its
abliterated variant, GLM-5.3-Flash and DeepSeek-V4.1-Flash-1017 as well. It is
the one whose answer is not eaten by a reasoning phase — on a real render asking
for every heading and button label it answered 7/7 headings + 3/3 labels in 4/4
runs at 0.56–2.49 s, where GLM-5.3-Flash needed ≥1,200 output tokens (5–7 s) and
Qwen3.8-27B returned no content in any run. The installer's comment,
`templates/config.yaml`'s header, the §4 row above and `release.json`'s notes all
say that now, and a Frontier-tier pin would take the image out of the enclave.
`merge_block` compares dotted key paths — with bare keys,
`auxiliary.compression.model` sits at the same indentation as
`auxiliary.vision.model` and the first version of the card deleted it on every
re-run (reproduced against `main`'s function in `docs/evidence/a2-vision-model.md`
§4). `--self-test` prints PASS with `ok the template pins the Private TEE vision
model`, `ok an existing tenant gains the vision pin and keeps compression` and
`ok a Frontier-tier vision pin is replaced and compression kept`, and a fresh
scratch tenant root made by `sim_tenant.py` in a private mount namespace ran
29/29 on this cut's installer (run 1 exited 0 in 3m 51s, its log naming installer
`5d9e9e76…`; run 2 read `tenant: same` with 0 write lines). Drifting that root's
pin back to `anthropic/claude-haiku-4-5` beside a `compression.model` Hermes owns,
one re-run wrote `(7 managed blocks; keys Hermes added are kept)` with the pin
back and `model: "vendor/compress-1"` intact; the next run printed `same …
(7 managed blocks, sha256 36bcafa039fe…)` and `tenant: same`. That root's own
`state.db` records the call as
`Qwen/Qwen3-VL-30B-A3B-Instruct|vision|1|1277|16|712` for a 1,440×900 screenshot
of the landing page (160,807 bytes) — 1,293 prompt tokens, 4.479 s from the
tool-call message to its result. `tools/recut_release.py` re-derived and re-signed
the pair; `--check` prints eleven `ok` lines. The installer is `5d9e9e76…`
(1,563,199 bytes) and the manifest `7c138f5a…` (50,044 bytes); the diff against
`-5`'s manifest is one line, `installer_sha256` — the `6d3f7be0…` audit row, the
`d9c4278a…` backup row, the 48 skill rows and `skills.tar.gz` (`27d98728…`,
852,651 bytes) are unchanged, so no fresh-root rebuild is claimed.
`--verify-signature --pubkey installer/odile-signing-key.pub` prints
`signature: OK (sha256 5d9e9e76…)`; a one-byte edit reads `93b849bb…`
(1,563,200 bytes) and `signature: BAD`, exit 3. `release/release.json`
(`c1bd54a3…`, 1,867 bytes) names `2026.11.22-6` at `2026-11-22T17:45:00Z`. It is
committed, not deployed: measured 2026-11-22T17:41Z the URL serves the deployed
`2026.11.19-5` (`1d946c44…`, 1,503,889 bytes; manifest `7031d4a0…`). The id
ledger: `-4` is t_509f85e4's support-grant help cut (15:30Z), `-5` was this
card's pre-review cut (16:35Z, superseded before any merge), and `-6` is later
than every cut by `generated_at` (17:45Z); no other ref carries `-6`.

**The `2026.11.23-2` cut (ODL-925: B-once — the first visit is a login).**
The first-visit page and the service it runs are re-embedded: `GET
/first-visit` is a sign-in form (one password field, no token, title `Sign in
— Odile`) when the `.env` already holds a usable sign-in, and the classic setup
page only when it does not; `?t=` answers 410 ("already set") for any value; the
state gains `first_login_at` beside `signin_set_at` in the one write; and the
page's footer carries D-1 and drops the A-era first-visit footer's two
retired clauses. The installer writes the seed's `sign_in_hash` into the
tenant `.env` before the Webapp's first boot, and the image's installer stamps
the seeded state, so the basic-auth gate accepts the registration password with
no page visited. The embedded copies are `installer/first_visit.py` `7ddf42c3…`
(76,304 bytes) and `web/first-visit.html` `98208a4c…` (45,248 bytes), pinned by
`FIRST_VISIT_SHA256` and `FIRST_VISIT_PAGE_SHA256`. The installer is
`36f1ecaf…` (1,573,463 bytes) and the manifest `e26f5fc6…` (50,044 bytes); the
manifest's diff against `2026.11.23-1` is `installer_sha256` and
`generated_at` alone — the `6d3f7be0…` audit row, the `d9c4278a…` backup row,
the 48 skill rows and `skills.tar.gz` `27d98728…` (852,651 bytes) are
unchanged, so no fresh-root manifest rebuild is claimed. `--self-test` prints
`self-test: PASS` on the checkout's installer and on
`release/odile_install.py`; `--verify-signature --pubkey
installer/odile-signing-key.pub` prints `signature: OK (sha256 36f1ecaf…)`; a
one-byte edit reads `f55bdb52…` (1,573,464 bytes) and `signature: BAD`, exit 3;
`tools/recut_release.py --check` prints eleven `ok` lines.
`release/release.json` (`6744ffaa…`, 1,144 bytes) names `2026.11.23-2` at
`2026-11-23T01:33:33Z`. It is committed, not deployed: measured
2026-11-23T01:27Z the URL serves the deployed `2026.11.19-5` (`1d946c44…`,
1,503,889 bytes; manifest `7031d4a0…`). The id ledger: `-1` is t_fe2aee5c's
gate-label cut (00:40Z), and `2026.11.23-2` is later than every cut by
`generated_at` (01:33:33Z) and on no other ref.

**The `2026.11.23-1` cut (t_fe2aee5c: the gate row names the file that runs
it).** `gate_ok`'s docstring, the row every install prints and a self-test
comment said the gate had "the same shape as deploy_demo_site.gate_ok" and was
"reused from deploy_demo_site.py". That path is on no ref either — not a
deletion, an absence:

```sh
$ git ls-files | grep deploy_demo_site; echo $?
1
$ git log --all --oneline -- deploy/demo-host/deploy_demo_site.py; echo $?
0
$ ls deploy/demo-host/deploy_demo_site.py
ls: cannot access 'deploy/demo-host/deploy_demo_site.py': No such file or directory
$ grep -rn "def gate_ok" --include=*.py deploy installer
installer/odile_install.py:15298:def gate_ok(anonymous_html: int, anonymous_api: int, authenticated: int) -> bool:
```

The table is asserted on this tree by `deploy/demo-host/check_demo_host.sh`:
`expect "GET / without credentials" "302"` (line 1279),
`expect "GET /api/sessions without credentials" "401"` (1320) and
`expect "GET / with the session cookie" "200"` (1349); the deploy's
`apply_demo_public.py` reads the same codes back in its restart leg. So the
docstring now says "as `deploy/demo-host/check_demo_host.sh` asserts it", the
row an install prints reads `the gate is 302/401/200
(deploy/demo-host/check_demo_host.sh shape)`, and `installer/sim_tenant.py`'s
docstring follows. One docstring, two comments and one printed string: no
managed config block, tenant file, skill or pin moved, so the re-cut leaves
`-7`'s manifest identical except for `installer_sha256` (the `6d3f7be0…` audit
row, the `d9c4278a…` backup row, the 48 skill rows and `skills.tar.gz`
`27d98728…` are unchanged) and no fresh-root manifest rebuild is claimed. The
installer is `48d5b48a…` (1,563,412 bytes) and the manifest `a263d35b…`
(50,044 bytes). `--self-test` prints `self-test: PASS` on the checkout's
installer and on `release/odile_install.py`; `--verify-signature --pubkey
installer/odile-signing-key.pub` prints `signature: OK (sha256 48d5b48a…)`; a
one-byte edit reads `c98364d5…` (1,563,413 bytes) and `signature: BAD`, exit 3;
`tools/recut_release.py --check` prints eleven `ok` lines.
`release/release.json` (`3bf023b1…`, 1,436 bytes) names `2026.11.23-1` at
`2026-11-23T00:40:00Z`. A fresh scratch root this installer made ran 29/29
(run 1 exit 0 in 4m 25s; run 2 `tenant: same`, 0 write lines) — the simulation
transcript above is that run, and its gate row prints the new name. It is
committed, not deployed: measured 2026-11-23T00:43Z the URL serves the deployed
`2026.11.19-5` (`1d946c44…`, 1,503,889 bytes; manifest `7031d4a0…`). The id
ledger: `-7` is t_69a749f0's docstring cut (2026-11-22T20:32Z), and
`2026.11.23-1` is later than every cut by `generated_at` (00:40Z) and on no
other ref.

**The `2026.11.22-7` cut (t_69a749f0: the docstring names a deployer that
exists).** The shipped installer's module docstring said the tenant install's
pure helpers "are the same ones `deploy/demo-host/deploy_demo_host.py` calls, so
the demo host and a tenant cannot drift apart". That path exists on no ref of
this repository — not a deletion, an absence:

```sh
$ git ls-files | grep deploy_demo_host; echo $?
1
$ git log --all --oneline -- deploy/demo-host/deploy_demo_host.py; echo $?
0
$ ls -1 deploy/demo-host/*.py
deploy/demo-host/apply_demo_icons.py
deploy/demo-host/apply_demo_public.py
deploy/demo-host/check_demo_public.py
deploy/demo-host/create_dns.py
deploy/demo-host/deploy_demo_icons.py
deploy/demo-host/deploy_demo_public.py
deploy/demo-host/tenant_console.py
```

The docstring now names the demo host's live deployer — `apply_demo_public.py`,
which runs as root on the tenant — and says which helpers are really shared:
`hash_password`/`verify_password` (the provider's scrypt format, `2**14, 8, 1,
32, 16` in both files) and `write_atomic` have independent copies there, while
the three-key `.env` allowlist (`TENANT_ENV_KEYS`, `count_env_violations`) and
the managed-block config merge (`merge_config`, `merge_block`, `MANAGED_KEYS`)
exist only in the installer. Measured with `grep -rn 'def merge_config'` and
`'def count_env_violations'` over `installer/` and `deploy/`: one hit each,
both in `installer/odile_install.py`. Docstring bytes only: no managed config
block, tenant file, skill or install-script pin moved.
`tools/recut_release.py` re-derived and re-signed the pair; `--check` prints
eleven `ok` lines. The installer is `430d31a7…` (1,563,383 bytes) and the
manifest `80ccb90b…` (50,044 bytes); the diff against `-6`'s manifest is one
line, `installer_sha256` — the `6d3f7be0…` audit row, the `d9c4278a…` backup
row, the 48 skill rows and `skills.tar.gz` (`27d98728…`, 852,651 bytes) are
unchanged, so no fresh-root rebuild is claimed.
`--self-test` prints `self-test: PASS` on the checkout's installer and on
`release/odile_install.py`; `--verify-signature --pubkey
installer/odile-signing-key.pub` prints `signature: OK (sha256 430d31a7…)`; a
one-byte edit reads `fd890e30…` (1,563,384 bytes) and `signature: BAD`, exit 3.
`release/release.json` (`76fb23a7…`, 1,378 bytes) names `2026.11.22-7` at
`2026-11-22T20:32:00Z`. It is committed, not deployed: measured
2026-11-22T20:26Z the URL serves the deployed `2026.11.19-5` (`1d946c44…`,
1,503,889 bytes; manifest `7031d4a0…`). The id ledger: `-6` is t_e7e5ac73's
vision-pin cut (17:45Z), and `-7` is later than every cut by `generated_at`
(20:32Z); no other ref carries `-7`.

**The `2026.11.22-3` cut (t_ab95ec2a, the person the help skill speaks in,
rebuilt on `main`'s `-2`).** ODL-474's brand follow-up: `skills/odile-help/SKILL.md`
is the surface the agent *speaks from*, so §3's blockquote and the briefing
paragraph under it say "you ask me to restore", "ask me to show you the file"
and "I can show it again" — BRAND.md §Voice ("the agent is *I*") and the
skill's own rule at line 13. Every word and fact is the deck's: `docs/CLAIMS.md`
§5 and A9 are unchanged, and `docs/FAQ.md`, `web/faq.html` and
`templates/emails/support.md` keep the deck's "the agent", because there the
reader is a third party. `tests/test_help_answers.py` pins both forms and
proves the spoken literal is the deck's answer with only the pronouns reversed;
it fails at `main`'s skill with the defect text and passes here.
While this card sat in review the branch fell 652 commits behind `main`;
rebuilt on `main`'s `5fc113c` (newest cut `2026.11.22-2`, ODL-469's brief
email), `main`'s installer and eight release names were taken whole,
`installer/embed_support.py` re-ran byte-identical (`f0411894…`, 53,769 bytes),
the tenant bundle re-embedded with this card's skill (source `cec38dc2…`,
6,256 bytes) and the installer was re-signed. The installer moves from
`87340eee…` (1,557,751 bytes) to `85282fd2…` (1,557,729 bytes); the diff
against `main`'s installer is four lines — the `odile-help` pin and the three
pronouns. `--self-test` PASSes and `--verify-signature` prints OK; a one-byte
edit reads `0815e953…` (1,557,730 bytes) and `signature: BAD`, exit 3.
The manifest was built from `/var/tmp/odile-ab95ec2a/r4/root`, the fresh
scratch root this installer itself made in a private mount namespace so no
concurrent simulation could unlink the shared seed ledger: `sim_tenant.py
--reset --apply` ran 29/29 (run 1 exited 0 in 3m 34s, run 2 read `tenant: same`
with 0 write lines), and the skill that root installs is byte-identical to the
checkout (`cmp` exit 0, `cec38dc2…`). `scripts/manifest.py build --root
/var/tmp/odile-ab95ec2a/r4/root --user odl474r4 --audit scripts/audit.py`
records the audit row (`6d3f7be0…`, byte-equal to the installed `odile-audit`),
and `scripts/manifest.py diff` against `-2`'s document prints
`installer_sha256` and three skill rows — `odile-first-run` `ea37ad3c…` →
`1c89d90b…`, `odile-help` `3800d71f…` → `f7f907a8…` and `odile-recipes`
`7824a794…` → `042ee31b…` — because `-1`/`-2` were surgical re-cuts that left
the skill rows at `2026.11.21-6`'s bytes while ODL-469 changed two of the three
skills. The document is `2c39128b…` (50,044 bytes, 48 skills, audit
`6d3f7be0…`) at `2026-11-22T12:45:00Z`; the skills tarball is `57c5f793…`
(852,657 bytes), and its `odile-help/SKILL.md` carries the first-person form
(`cec38dc2…`).
`release/release.json` (`3b19de25…`, 1,271 bytes) names release `2026.11.22-3`,
installer `85282fd2…` and manifest `2c39128b…`; `release/odile_install.py` is the
checkout's installer and `release/odile_install.py.sig` its detached signature;
both JSON signatures verify against `installer/odile-signing-key.pub`, and
`tools/recut_release.py --check` prints eleven `ok` lines. A second cut into
`/var/tmp/odile-ab95ec2a/r4/again` reproduced all eight names byte for byte,
and the manifest rebuilt from the same root at the same timestamp is
byte-identical. It is committed, not deployed: measured 2026-11-22T12:38Z,
`https://odile.fyi/release.json` serves the deployed `2026.11.19-5` (installer
`1d946c44…`, 1,503,889 bytes; manifest `7031d4a0…`), and the next
`./scripts/deploy.sh` from a `main` that carries this merge publishes `-3`. The
id ledger: `-2` is ODL-469's brief-email cut (08:47Z) and `-3` is later than
every cut by `generated_at` (12:45Z) and was carried by no other ref.
While this card was in review `main` moved again to `a5e5d7b` (t_7378e496's
brief-delivery fix); that merge moves no `installer/`, `release/` or `skills/`
byte (`git diff 5fc113c..a5e5d7b -- installer/ release/ skills/` is empty), the
merged tree's installer is still `85282fd2…` and its `release/release.json`
still names `2026.11.22-3`, and `git merge-tree --write-tree main
odl/t_ab95ec2a` exits 0. A later `main` that carries this merge therefore still
ships the `-3` pair; re-check `git diff <cut-base>..main -- installer/ release/`
before merging.

**The `2026.11.22-4` cut (t_509f85e4, the support-grant answer's person).**
ODL-474's second brand follow-up: §5 is the other answer the agent says aloud,
and it still called the agent "the agent". `skills/odile-help/SKILL.md` now says
"my memory and past prompts"; A10's row and `docs/CLAIMS.md` §5, `docs/FAQ.md`,
`web/faq.html` and `templates/emails/support.md` keep "the agent's memory",
because the reader there is a stranger — the deck is unchanged.
`tests/test_help_answers.py` pins the deck's A10 text on all three desk
surfaces, the spoken form on the skill, and proves the spoken paragraph is the
deck's facts with only the possessive reversed (`third_person()` now maps `my`
as well as `me`/`I`); it fails at `main`'s skill with the defect text and
passes here. `installer/embed_tenant.py` re-embedded the tenant bundle (source
`040d8de0…`, 6,247 bytes) and the installer was re-signed: `f6761770…`
(1,557,720 bytes), nine bytes under `-3`'s `85282fd2…` (1,557,729) — the
pronoun and its line wrap, nothing else. `--self-test` prints `self-test:
PASS`; `--verify-signature --pubkey installer/odile-signing-key.pub` prints
`signature: OK (sha256 f6761770…)`; a one-byte edit reads `dfa3dfbc…`
(1,557,721 bytes) and `signature: BAD`, exit 3. The manifest was built from
`/var/tmp/odile-509f85e4/root`, the fresh scratch root this installer itself
made in a private mount namespace (the shared seed ledger bind-mounted from a
copy): `sim_tenant.py --reset --apply` ran 29/29 (run 1 exited 0 in 4m 43s,
run 2 read `tenant: same` with 0 write lines), and the skill that root installs
is byte-identical to the checkout (`cmp` exit 0, `040d8de0…`).
`scripts/manifest.py build --root … --user odl509r1 --audit scripts/audit.py`
records the audit row (`6d3f7be0…`, unchanged); the document is `3e7c99a4…`
(50,044 bytes, 48 skills) at `2026-11-22T15:30:00Z`, and `scripts/manifest.py
diff` against `-3` prints two lines — `installer_sha256` and the `odile-help`
skill row (`f7f907a8…` → `aa64ace9…`). A rebuild from the same root at the
same timestamp is byte-identical to it. `release/release.json` (`2319d51a…`,
1,121 bytes) names release `2026.11.22-4`, installer `f6761770…` and manifest
`3e7c99a4…`; `release/odile_install.py` is the checkout's installer and
`release/odile_install.py.sig` its detached signature; both JSON signatures
verify against `installer/odile-signing-key.pub`; the skills tarball is
`27d98728…` (852,651 bytes), and its `odile-help/SKILL.md` carries the spoken
form. `tools/recut_release.py --check` prints eleven `ok` lines, and a second
cut into `/var/tmp/odile-509f85e4/again` reproduced all eight names byte for
byte. It is committed, not deployed: measured 2026-11-22T15:22Z,
`https://odile.fyi/release.json` serves the deployed `2026.11.19-5` (installer
`1d946c44…`, 1,503,889 bytes; manifest `7031d4a0…`), and the next
`./scripts/deploy.sh` from a `main` that carries this merge publishes `-4`. The
id ledger: `-3` is t_ab95ec2a's first-person cut (12:45Z) and `-4` is later
than every cut by `generated_at` (15:30Z).

**The `2026.11.22-2` cut (ODL-469: the brief email is wired into the tenant,
re-embedded on `main`'s late-sentence follow-up).** This branch is rebased onto
`main`'s `ff1c6b0` (t_55813b46's merge, 2026-11-22T08:26Z), which changed
`scripts/send_brief.py` to `581a9b88…` after this card's first base; the
installer's embedded copy and pins are regenerated from that checkout, so a
fresh tenant gets `main`'s sender. For the merge gate the branch is rebased
again onto `main`'s `9ec0873` (through `60e195e`); no installer byte moved in
that rebase, and `tests/test_keys.py`'s seed fixture gained the `tls_ca` the
new contract requires. Its product surface is the same as the
`2026.11.22-1` cut below — the two pinned tenant files (0755 / 0644), the
seed's `mail_relay.tls_ca` cached at `/etc/odile/mail-ca.pem`, the ritual's
address question, and the `Morning brief` prompt's final sender command — and
the only difference is `main`'s sender: the late sentence now follows the
dispatch's own local day, and `on_time` records `late_seconds`.
`tools/recut_release.py` re-derived and re-signed the pair over `main`'s
`2026.11.21-6`: the installer is `87340eee…` (1,557,751 bytes) and
`manifest.json` `566996e7…` (50,044 bytes); against `-1`'s document the `diff`
is one line, `installer_sha256` (the manifest's size is unchanged), so this is
a surgical re-cut and not a root rebuild — the skills artifact (`a813f1de…`,
852,004 bytes), the manifest's skill rows, the `6d3f7be0…` audit row, the
`d9c4278a…` backup row and the `ReadWritePaths=/run` unit are `main`'s.
`release/release.json` (`530f589c…`, 1,202 bytes) names `2026.11.22-2` at
`2026-11-22T08:47:59Z`; its note says the manifest's two Odile skill rows and
`skills.tar.gz` stay `-6`'s until the next fresh-root cut. `--self-test`
prints `self-test: PASS` on the checkout's installer and on
`release/odile_install.py`; `--verify-signature` prints
`signature: OK (sha256 87340eee…)` on both; a one-byte edit prints
`signature: BAD (sha256 8e387623…)` and exits 3; and
`tools/recut_release.py --check` prints eleven `ok` lines. Measured
2026-11-22T08:49Z the URL serves `2026.11.19-5` (`1d946c44…`, 1,503,889 bytes;
manifest `7031d4a0…`), and a deploy from a `main` that carries this cut is what
moves it. The id ledger: `-1` was this card's pre-rebase cut (04:50Z,
superseded before any deploy) and `2026.11.22-2` is later than every cut before
it by `generated_at` (08:47Z). §2 of `image.md` remains the `-3` bench
measurement; a build of this tree carries the `2026.11.22-2` pair.

**The `2026.11.22-1` cut (ODL-469: the brief email is wired into the
tenant).** This branch is rebased onto `main`'s `b000999`, whose newest cut is
`2026.11.21-6` (t_df24b754's symlinked-root cut above), so the brief bundle and
the seed's relay CA are in one installer. Its product surface is the tenant
path: `installer/embed_tenant.py` pins `scripts/send_brief.py` →
`$HERMES_HOME/scripts/send_brief.py` (0755) and `templates/brief.txt` →
`$HERMES_HOME/brief.txt` (0644) beside `AGENTS.md` and the three skills, the installer
writes both and caches the seed's `mail_relay.tls_ca` at
`/etc/odile/mail-ca.pem` (0644), `--self-test` compares the two bundled files
with the checkout, `seed/seed.py` adds `mail_relay.tls_ca` to the signed seed (a
config without it, or with a non-PEM in it, refuses to start), and the
`Morning brief` prompt ends with `sudo -n /usr/bin/python3
~/.hermes/scripts/send_brief.py` in the cron template, both skills and the
installer's embedded copies. On a fresh simulation root the installed files are
the checkout's bytes at those two modes (`e58aa602…`, `715bf2a2…`), the second
run reads `tenant: same` (29/29), the ritual writes `~/.hermes/brief.json`, and
the job it creates fired on its slot (`kind on_time`, 2.3 s late) and sent one
message whose receipt passes SPF, DKIM and DMARC (Gmail filed that one to Spam;
the placement is its own card). `tools/recut_release.py` re-derived and
re-signed the pair over `main`'s `2026.11.21-6`: the installer is `b12e9273…`
(1,556,716 bytes) and `manifest.json` `600bdcb7…` (50,044 bytes); against `-6`'s
document the `diff` is one line, `installer_sha256`, so this is a surgical re-cut
and not a root rebuild — the skills artifact (`a813f1de…`, 852,004 bytes), the
manifest's skill rows, the `6d3f7be0…` audit row, the `d9c4278a…` backup row and
the `ReadWritePaths=/run` unit are `main`'s. `release/release.json` (`4a831485…`,
1,125 bytes) names `2026.11.22-1` at `2026-11-22T04:50:00Z`; its note says the
manifest's two Odile skill rows and `skills.tar.gz` stay `-6`'s until the next
fresh-root cut. `--self-test` prints `self-test: PASS` on the checkout's
installer and on `release/odile_install.py`; `--verify-signature` prints
`signature: OK (sha256 b12e9273…)` on both; a one-byte edit prints
`signature: BAD (sha256 61f4b0c6…)` and exits 3; and
`tools/recut_release.py --check` prints eleven `ok` lines. Measured
2026-11-22T06:34Z the URL serves `2026.11.19-5` (`1d946c44…`, 1,503,889 bytes;
manifest `7031d4a0…`), and a deploy from a `main` that carries this cut is what
moves it. The id ledger: `-6` is t_df24b754's symlinked-root cut (01:24Z) and
`2026.11.22-1` is later than every cut before it by `generated_at` (04:50Z).
§2 of `image.md` remains the `-3` bench measurement; a build of this tree
carries the `2026.11.22-1` pair.

**The `2026.11.21-5` cut (t_d55b0e47: the `/verify` unit's sandbox keeps
cryptsetup's lock writable).** This branch is rebased onto `main` `4d88408`
(t_84e94f4b's disk-snapshot merge, which moves no installer byte and whose
newest cut is still `2026.11.21-4`, t_87943990's boot-key walk cut above; its
`2026.11.21-2` is t_bf6092d7's gateway start-input cut), so both fixes are in
one installer (`git grep -c gateway_start_inputs installer/odile_install.py` =
2, and the `/run` assertion below). The tenant `/verify` page's LUKS row read
FAIL on every machine that runs the unit the image and the installer write: the
program runs `cryptsetup status root_crypt` and then `cryptsetup luksDump
<device>`, cryptsetup takes its read lock in `/run/cryptsetup`, and the unit's
`ProtectSystem=strict` mounts `/run` read-only. Measured on the demo host on
2026-11-21, under the unit's own four properties (`NoNewPrivileges=true`,
`PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, no
`ReadWritePaths=`): `systemd-run … /usr/sbin/cryptsetup luksDump /dev/sda3`
printed `Failed to acquire read lock on device /dev/sda3.` and `Device /dev/sda3
is not a valid LUKS device.` (exit 5), while the same command outside the
sandbox prints the header — one keyslot, slot 2, no tokens — and the page
rendered `LUKS FAIL cannot read the root container's LUKS header`. It is not a
regression of the move to `odile-verify`: `deploy/image/odile-first-visit.service`
carries the same four properties (`ReadWritePaths=__TENANT_HOME__ /etc/odile`,
no `/run`), so the old 8091 surface read FAIL too. Both copies now carry
`ReadWritePaths=/run` — `deploy/image/odile-verify.service` (the image's copy,
which `install-stack.sh` installs) and the installer's `VERIFY_UNIT_TEMPLATE` —
and `--self-test` asserts it beside the existing `ProtectSystem=strict` check.
`/run`, not `/run/cryptsetup`, because a `ReadWritePaths=` path missing at start
fails the unit (measured: `systemd-run --property=ProtectSystem=strict
--property=ReadWritePaths=/run/odile-no-such-dir /bin/true` → exit 226) and a
machine with no LUKS container has no `/run/cryptsetup` at boot. Under the same
four properties plus `ReadWritePaths=/run` the same `systemd-run` prints the
header and exits 0.
`tools/recut_release.py` re-derived and re-signed the pair from this head: the
installer is `9b585209…` (1,527,037 bytes) and `manifest.json` is `3912ce1b…`
(50,044 bytes); against `main`'s `2026.11.21-4` document its `diff` is one
line, `installer_sha256`, so this is a surgical re-cut and not a root rebuild —
the skills artifact (`a813f1de…`, 852,004 bytes), the `odile-help` row, the
`fd66f284…` handoff audit, the `d9c4278a…` backup row and t_87943990's
find-based boot-key walk are `main`'s, unchanged. `release/release.json`
(`81bab52c…`, 2,198 bytes) names `2026.11.21-5` at `2026-11-21T23:25:00Z`.
`--self-test` prints `self-test: PASS` on both the checkout's installer and
`release/odile_install.py`; `--verify-signature` prints
`signature: OK (sha256 9b585209…)` on both; a one-byte edit prints
`signature: BAD (sha256 df5283aa…)` and exits 3; and
`tools/recut_release.py --check` prints eleven `ok` lines.
The demo host proved it from this installer's own unit file: this cut's pair was
put at `/usr/local/bin/odile-install.sh` and
`/usr/local/lib/odile-image/odile_install.py.sig` (sha256 `9b585209…` and
`172cfbdf…` on the machine), the done marker removed, and
`odile-release-setup.sh --force` run in a transient unit. The run ended `rc=0`
in 9 s with `"installer_sha256": "9b585209…", "ok": true`; the log's
`odile-verify` block is `restart odile-verify (changed)` and
`listening verify 127.0.0.1:8098`. `/etc/systemd/system/odile-verify.service`
is byte-identical to this installer's `VERIFY_UNIT_TEMPLATE` rendered for that
machine (`cmp`, 1,444 bytes, sha256 `dbe252cd…`; the pre-fix render was 962
bytes, `5ef7f147…`), `systemctl show -p ReadWritePaths` prints `/run`, and the
unit is `active`, `Result=success`, `ExecMainStatus=0`. The run's `smoke` row is
`false` because the chat/shell/file/cron smoke was already recorded at
2026-11-21T20:00:38Z and the forced run prints `same chat/shell/file/cron smoke
recorded` rather than spend another paid turn. On `http://127.0.0.1:8098/verify`,
read over the tenant console, the page had 21 rows with the LUKS row `PASS one
keyslot (slot 2), no token: one volume key, no second way in`, `seal policy`
`0x30000` PASS and `sign-in hash` PASS; after the deploy re-injected the demo
lede and its `--expect` section the public page still had 21 rows (16 PASS, 3
CHECK, 2 NOTE) and the same three verdicts, and `check_demo_public.py --target
<the address demo.odile.fyi resolves to>` passed 73/0, `check_demo_host.sh
--tenant` 113/0. One interaction the next operator must know: the forced run
rewrites `/home/tenant/.hermes/config.yaml` and `.env`, so `webapp.public_url`
goes back to the machine's slug and a sign-in on the demo name answers 400
until `deploy_demo_public.py` re-sets it (measured: `tenant_console.py --base
https://<the machine's own name>` answered while the demo name answered 400;
after the deploy the demo name answered 200 with the judge session). The `.env`'s
sign-in hash row read PASS before and after. It is committed, not deployed:
measured 2026-11-21T23:22Z the URL serves `2026.11.19-5` (`1d946c44…`,
1,503,889 bytes; manifest `7031d4a0…`) until a deploy from a `main` that carries
this cut. The id ledger: `-1` to `-4` are the 2026.11.20 cuts listed above,
`2026.11.21-1` is t_9574a1c8's help-routing cut (11:13Z), `2026.11.21-2` is
t_bf6092d7's gateway start-input cut (16:45Z), `2026.11.21-3` was this card's
first `/verify`-unit cut (19:05Z, superseded by this round), `2026.11.21-4` is
t_87943990's boot-key walk cut (22:10Z), and `2026.11.21-5` is later than every
cut before it by `generated_at` (23:25Z). §2 of `image.md` remains the `-3`
bench measurement; a build of this tree carries the `2026.11.21-5` pair.

**The `2026.11.21-4` cut (t_87943990: the handoff audit's boot-key walk).**
This branch merges `main`'s `1a7c7d8`, whose `2026.11.21-2` is t_bf6092d7's
gateway start-input cut; its product surface is the `/verify` page's cost, not a
new row. `scripts/audit.py` now enumerates the boot-key candidates with
`find(1)` — same four roots, same 200..65536-byte window, same
`.pub`/`authorized_keys*`/`known_hosts*` exclusions — and reads each candidate's
first 48 bytes with `head(1)` from a `tee`'d path list, so the read overlaps the
walk; symlinks are enumerated too and followed on their target in Python, and the
old `os.scandir` walk remains the fallback when find, tee, xargs or head cannot
answer or the stream does not line up. The row's proof is unchanged: the same
`PRIVATE KEY` marker in the first 48 bytes, the same `ssh-keygen -y` (or `.pub`
sibling) identification, the same FAIL text. `WALK_TIMEOUT` now bounds the
`communicate` read and kills the process group, so a `find` hung on an
unresponsive mount cannot hold the page, and the dead `_private_candidates`
helper is gone. `verify/verify.py` starts the audit before it reads its other
sources, so the page pays the walk once instead of the walk plus the rest.
On the demo tenant (2 vCPU, SEV-SNP) this took loopback
`http://127.0.0.1:8098/verify` from 6.2 s to 2.037 / 1.981 / 1.948 s (three
fetches after the cut's forced release-setup). The boot-key scan is
1.77–1.83 s of that and the audit alone 1.91–1.96 s, both measured over the
tenant console: 151,550 candidate files have their first 48 bytes opened
(`head` alone is 1.5 s of the 1.8), so the page sits on the card's 2 s line
rather than under it on this guest. The scan is 1.8 s against the 6.3 s
Python walk, and the remaining ~0.2 s is the rest of the page, overlapped;
the audit's 10 rows are unchanged.
`tools/recut_release.py` re-derived and re-signed the pair over `main`'s
`2026.11.21-2`: the installer is `934fe8f6…` (1,525,933 bytes) and
`manifest.json` `36fcbdc1…` (50,044 bytes); against `-2`'s document the `diff`
is two lines, `installer_sha256` and `audit_sha256` (the row is now re-derived
from the installer the cut ships, so the signed document names the audit program
a stranger runs), so this is a surgical re-cut and not a root rebuild — the
skills artifact (`a813f1de…`, 852,004 bytes), the `odile-help` row, the
`d9c4278a…` backup row and t_bf6092d7's gateway start-input fix are `main`'s.
`release/release.json` (`daa46803…`, 1,716 bytes) names `2026.11.21-4` at
`2026-11-21T22:10:00Z`; `2026.11.21-3` was t_d55b0e47's first `/verify`-unit
cut, and that branch re-cut as `-5` above.
`--self-test` prints `self-test: PASS` on the checkout's installer and on
`release/odile_install.py`; `--verify-signature` prints
`signature: OK (sha256 934fe8f6…)` on both; a one-byte edit prints
`signature: BAD (sha256 2a220f54…)` and exits 3; and
`tools/recut_release.py --check` prints eleven `ok` lines. The served URL still
served `2026.11.19-5` at 2026-11-21T22:09Z, and a deploy from a `main` that
carries this cut is what moves it.

**The `2026.11.21-2` cut (t_bf6092d7: the gateway's key-file ordering).** This
branch merges `main`'s `617208e`, whose `2026.11.21-1` is t_9574a1c8's
help-routing cut above; the merge's only product surface is the installer's
service convergence, because both sides' embedded bundles land cleanly
(`installer/embed_tenant.py` re-embeds `skills/odile-help/SKILL.md` at
`ace83094…`, 6,278 bytes, main's bytes beside this card's tenant pages). On a
first boot the image's own `odile-agent.service` is already running when the
installer writes `/etc/odile/key.env`, so the gateway that exec'd before that
file existed keeps an environment without
`HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY` and the smoke's chat row answers
`Provider said: HTTP 401: Invalid or expired API key` for a key that answers
200 from HQ (measured on the demo tenant's first boot, t_bf6092d7). The
installer now treats `/etc/odile/key.env` as a gateway start input
(`gateway_start_inputs`), restarts a unit whose inputs moved rather than
starting it (systemd can still report that unit `activating`, and `systemctl
start` is a no-op on the process already coming up), and reads the running
gateway's own `/proc/<MainPID>/environ` — the PID from `systemctl show -p
MainPID` — so a pass that finds every file `same` still restarts a gateway that
never read the key; the run prints which input moved or which variable the
process lacks, and a converged machine prints `same odile-agent active`.
`tests/test_installer_copy.py`'s `GatewayKeyOrderTest` pins the four states
and 4/4 fail against `main`'s installer bytes (`86e87a86…`), the
`AttributeError` naming the absent `gateway_start_inputs`.
`tools/recut_release.py` re-derived and re-signed the pair from the merged
tree: the installer is `1536ea2f…` (1,517,026 bytes) and `manifest.json`
`2f227d34…` (50,044 bytes); against `main`'s `2026.11.21-1` document its
`diff` is one line, `installer_sha256`, so this is a surgical re-cut and not a
root rebuild — the skills artifact (`a813f1de…`, 852,004 bytes), the
`odile-help` row (`3800d71f…`), the `539e205f…` handoff audit and the
`d9c4278a…` backup row are `main`'s, unchanged.
`release/release.json` (`2915f480…`, 1,997 bytes) names `2026.11.21-2` at
`2026-11-21T16:45:00Z`; the `2026.11.20-5` cut this branch carried into the
first four rounds (06:33Z) was superseded before any deploy, because `main` cut
`-4` and then `2026.11.21-1` while the branch waited, and one merged tree
carries one cut id per artifact. `--self-test` prints `self-test: PASS` on both
the checkout's installer and `release/odile_install.py`; `--verify-signature`
prints `signature: OK (sha256 1536ea2f…)` on both; a one-byte edit prints
`signature: BAD (sha256 6b91ee5e…)` and exits 3; and
`tools/recut_release.py --check` prints ten `ok` lines. The ordering is proved
on this host's real systemd at this head: with `main`'s bytes the stand-in
gateway keeps its MainPID and `/proc/<MainPID>/environ` carries no key
(verdict: the smoke would answer 401), while this pair prints
`restart odile-agent (the running gateway has no
HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY)`, moves MainPID to a process whose
environment carries the key with the file's own sha256 (`d16e833b…`), and a
second pass prints `same odile-agent active` with MainPID unchanged. A clean
simulator install at this head prints 29/29 with the smoke's chat row
`one chat turn straight to NEAR AI: exit 0, answer '<the user's name>'` beside
`"smoke": true` and `"installer_sha256": "1536ea2f…"`, and its second run
prints `tenant: same` with 0 writes. §10's note tells a `401` in that row apart
from an expired key: ask the process, not the file. It is committed, not
deployed: measured 2026-11-21T17:03Z the URL serves `2026.11.19-5`
(`1d946c44…`, 1,503,889 bytes; manifest `7031d4a0…`). The id ledger: `-1` to
`-4` are the 2026.11.20 cuts listed above, `2026.11.20-5` was this branch's
pre-review cut, `2026.11.21-1` is t_9574a1c8's help-routing cut (11:13Z), and
`2026.11.21-2` is later than every cut before it by `generated_at`
(2026-11-21T16:45Z). §2 of `image.md` remains the `-3` bench measurement; a
build of this tree carries the `2026.11.21-2` pair.


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
