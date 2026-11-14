# The signed app-layer manifest

ODL-214. `scripts/manifest.py` builds one JSON document that makes the app
layer pinnable — the Hermes commit, the hash the installer required of the
fetched `install.sh`, every installed Python and npm package version, the
managed toolchain, the OS package versions, every skill hash, the installer's
own sha256 and the handoff audit script — signs it with Odile's
Ed25519 key and serves it at `/manifest`.

**This is an operator signature, not a hardware measurement.** Measured boot
covers firmware, kernel, initramfs and the command line. The CPU does **not**
measure Hermes, the installer or the skills; this document is Odile saying
"these are the bytes we installed" (CLAIMS.md gap 1). Provenance, not
innocence: a valid signature says Odile built it, not that it is safe.

The machine-free half is proved here: two fresh scratch-root installs on HQ
produce the same manifest except `generated_at`, the signature verifies with the
published key, a changed file breaks the check and the listener serves
`/manifest` with `cache-control: no-store`. The tenant copy — installing the
signed document on a real tenant, serving it through the tenant's Caddy and
checking it against the installed tree — is ODL-332's evidence.

## 1. Committed files

| File | Role |
|---|---|
| `scripts/manifest.py` | build, sign, verify, diff and serve; standard library plus the system `openssl` |
| `tests/test_manifest.py` | 34 hermetic tests: two roots, the signature, a changed file, the install-script pin, the HTTP surface |
| `docs/runbooks/manifest.md` | this page |
| `installer/odile-signing-key.pub` | the published public key; the recovery kit carries the same bytes |
| `/root/.odile/installer-signing.pem` | the HQ private key, mode 0600, never in git |

The private key is the same one that signs `installer/odile_install.py` and the
seed responses. One key for one operator identity; rotating it means replacing
the public key in the repository — the URL serves that file from the deployed
tree, so a deploy publishes it — and in every recovery kit (see §9).

## 2. What it records

`build` reads an **installed root**, never a checkout: the point is to record
what the installer put on the machine.

| Field | Meaning |
|---|---|
| `installer_sha256` | the installer file that made the machine (`/usr/local/bin/odile-install.sh` on a tenant; `--installer` names another copy) |
| `audit_sha256` | the handoff audit script `/usr/local/bin/odile-audit` (ODL-208), the one the installer places and `/verify` quotes |
| `hermes.commit` | the Hermes checkout's `HEAD`, checked against the Webapp bundle's `pinnedCommit`; a mismatch is refused |
| `hermes.install_script_sha256` | the sha256 the installer required of the fetched `install.sh` before running it (ODL-399); read from the installer's own `--manifest`, which refuses one that predates the pin |
| `hermes.lockfiles` | sha256 of `package-lock.json` and `uv.lock` |
| `hermes.python_packages` | every `name==version` in the live Hermes venv's dist-info |
| `hermes.node_packages` | every resolved `name==version` in `package-lock.json` |
| `hermes.webapp` | `node`, the bundle's input `sourceHash` and output `outputHash` |
| `skills` | one content hash per installed skill directory |
| `skills_bundle_sha256` | sha256 of `.bundled_manifest` |
| `packages.os` | the installer's package set with an exact version each |
| `packages.toolchain` | the Hermes-managed toolchain pins: version and digest per package |
| `packages.missing` | names a scratch build could not resolve; a tenant build refuses instead |
| `model` | the model pin from the guest's seed cache |
| `generated_at` | the only timestamp in the document |
| `schema` | `1` |

Skill hashes are over a **canonical** form of the files: the installer renders
the tenant user and the home path into `odile-home`, and those are machine
layout, not app layer. `canonical_skill_bytes` substitutes `<user>`, `<home>`
and `<root>` before hashing, so a tenant and the release it was installed from
can agree on the same skill hash.
No secret is recorded: the seed's model name is copied, its NEAR key is not.

## 3. Build

A tenant, as root, after the install (ODL-332 installs the copy this runs):

```sh
./scripts/manifest.py build --root / --user odile \
    --installer /usr/local/bin/odile-install.sh \
    --audit /usr/local/bin/odile-audit \
    --out /etc/odile/manifest.json
```

`--installer` is the copy the user actually ran; CLAIMS.md §4.6 names
`/usr/local/bin/odile-install.sh`, so ODL-332 puts it there. `--audit` exists
only after ODL-208 has installed the handoff audit; until then omit it and
`audit_sha256` is `null`.

The scratch simulation, from the worktree:

```sh
./scripts/manifest.py build --root /var/tmp/odl214-a --out /tmp/a.json
```

`--generated-at 2026-11-08T14:00:00Z` fixes the timestamp for a reproducible
diff. `--packages-json` supplies a fixed version map instead of probing. A
missing installer, Hermes checkout, Webapp bundle, venv, lockfile or
skill is an error that names the path; nothing is silently omitted.

## 4. Two fresh installs (DoD line 1)

Two scratch installs from ODL-203's reviewed simulation — roots
`/var/tmp/odl214-a` (user `odl214a`, Webapp 9160, meter 9161) and
`/var/tmp/odl214-b` (user `odl214b`, Webapp 9162, meter 9163), one scratch
seed at `127.0.0.1:8085`, both exits 0 in 3m27s and 3m41s — then:

```sh
$ ./scripts/manifest.py build --root /var/tmp/odl214-a --generated-at 2026-11-08T14:00:00Z --out odl214-a.json
manifest     odl214-a.json (sha256 2f4c48cdc5ab8c764f7530adcd1b94edb3b10be2bedee1eff3cbdfa87384d05b, 45 skills, 9 packages, audit null)
$ ./scripts/manifest.py build --root /var/tmp/odl214-b --generated-at 2026-11-08T14:00:01Z --out odl214-b.json
manifest     odl214-b.json (sha256 de184be3e0fd4e07dceaee9421a7a7583e8ad8535251d0ee626b5a9d0d543dc8, 45 skills, 9 packages, audit null)

$ diff <(sed s/14:00:00Z/STAMP/ odl214-a.json) <(sed s/14:00:01Z/STAMP/ odl214-b.json)
$ echo $?
0

$ ./scripts/manifest.py diff odl214-a.json odl214-b.json
same (except generated_at)
```

The two documents are identical apart from `generated_at`; the differing
sha256 is the timestamp alone. The relation that matters is the app layer:
`installer_sha256 a666f7c8…`, `meter_sha256 3db4effb…`,
`hermes.commit 2bd378ed1b8d…`, the Webapp `sourceHash 1faeb528…` and
`outputHash a9fd70c5…`, 45 skill hashes, 104 Python pins and 1,216 npm pins —
all equal between the roots. The installer's own `--manifest` prints the same
`installer_sha256` and commit, so the two records agree.

**This transcript is historical (2026-11-08).** `a666f7c8…` is the ODL-214-era
installer that made `/var/tmp/odl214-a` and `-b`; `installer_sha256` means
"the installer file that made this root", so it moves whenever the installer
does. The reviewed ODL-214 head recorded `e2d5495d…`; at main `eb0a3b6`
(2026-11-09) the file is `371588b0…` (229,141 bytes, `--verify-signature` OK),
and at the ODL-399 head it is `ee4bbd41…` (257,577 bytes, `--verify-signature`
OK).
For what a fresh pair of roots would print, run `sha256sum
installer/odile_install.py` — the manifest agrees with the copy that built the
root, not with a release constant. A root made before ODL-399 is refused by
`build` (`records no hermes_install_sha256; it predates the Hermes
install-script pin`), because a manifest that cannot name the script that ran is
not this document; re-run the installer to give the root the pin.

## 5. Sign and verify (DoD line 2)

```sh
$ ./scripts/manifest.py sign --manifest odl214-a.json --key /root/.odile/installer-signing.pem
signature    odl214-a.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8, sha256 2f4c48cdc5ab8c764f7530adcd1b94edb3b10be2bedee1eff3cbdfa87384d05b)

$ ./scripts/manifest.py verify --manifest odl214-a.json --signature odl214-a.json.sig \
      --pubkey installer/odile-signing-key.pub
signature: OK (sha256 2f4c48cdc5ab8c764f7530adcd1b94edb3b10be2bedee1eff3cbdfa87384d05b, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
manifest     schema 1 · 45 skills · hermes 2bd378ed1b8d · deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated
$ echo $?
0
```

The fingerprint printed from the private key and from the published key agree,
and the recovery kit carries the published key byte for byte:

```sh
$ jq -j .signing_public_key /var/tmp/odl214-a/bootstrap/recovery-kit.json \
      | diff - installer/odile-signing-key.pub
$ echo $?
0
```

The signature is detached and over the **exact** bytes of the document: sign
the file, serve the file, verify the file. `verify` defaults to
`installer/odile-signing-key.pub`, so the published key is the one a stranger
uses. The URL `https://odile.fyi/odile-signing-key.pub` (CLAIMS.md §4.6)
serves those same bytes: `app.py` reads `installer/odile-signing-key.pub` from
the deployed tree on every request. Measured 2026-11-09 from HQ: `HTTP/2 200`,
`content-type: application/x-pem-file`, `cache-control: public, max-age=300`,
113 bytes, `sha256 13e8c2ae…`, `cmp`-equal to this file.

## 6. A changed file breaks the check (DoD line 3)

One appended line in one installed skill, then a rebuild:

```sh
$ printf 'tampered\n' >> /var/tmp/odl214-a/home/odl214a/.hermes/skills/note-taking/obsidian/SKILL.md
$ ./scripts/manifest.py build --root /var/tmp/odl214-a --generated-at 2026-11-08T14:00:00Z --out odl214-a-tampered.json
manifest     odl214-a-tampered.json (sha256 e9c0e208b51b37ff3434bbaf49aa396cad52eb8b6f10237b9f148a4ae09829a9, 45 skills, 9 packages, audit null)

$ ./scripts/manifest.py verify --manifest odl214-a-tampered.json --signature odl214-a.json.sig \
      --pubkey installer/odile-signing-key.pub
signature: BAD (sha256 e9c0e208b51b37ff3434bbaf49aa396cad52eb8b6f10237b9f148a4ae09829a9, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
$ echo $?
3
```

`diff` names the changed row — `note-taking/obsidian` moved from `b94764d4…`
to `6f7312d4…` — and nothing else moved. Restoring the file and rebuilding
returns the signed digest `2f4c48cd…`, so the check is about content, not the
order of the build. A byte flipped inside the signed JSON is caught the same
way (`verify` exits 3); the timestamp is inside the signature too.

## 7. Serve /manifest (DoD line 4)

The guest-facing half is a loopback listener; Caddy terminates TLS and proxies
to it, the way it does for the heartbeat. Transcript from HQ:

```sh
$ ./scripts/manifest.py serve --manifest odl214-a.json --signature odl214-a.json.sig --port 8093
listening    127.0.0.1:8093 (pid 1780092)

$ curl -sS -D- http://127.0.0.1:8093/manifest -o odl214-served.json
HTTP/1.0 200 OK
Server: odile-manifest/0.1 Python/3.14.4
Content-Type: application/json; charset=utf-8
Content-Length: 49630
X-Content-Type-Options: nosniff
Cache-Control: no-store

$ cmp odl214-served.json odl214-a.json && echo "the served bytes are the signed bytes"
the served bytes are the signed bytes
$ ./scripts/manifest.py verify --manifest odl214-served.json --signature odl214-a.json.sig \
      --pubkey installer/odile-signing-key.pub
signature: OK (…, sha256 2f4c48cd…)
```

`/manifest.sig` serves the detached signature with the same headers,
`/health` returns `ok`, and every other path is `404`. The listener does not
log requests and does not re-serialise: the body is the file's bytes.

### The tenant's Caddy block (ODL-332/ODL-472)

The installer's site template (ODL-203) gains these two routes before the
catch-all whenever the machine holds the signed pair; without a pair a classic
install keeps the Webapp catch-all and Caddy never proxies to a dead port.
`ensure_caddy_site` decides from `/etc/odile/manifest.json` and
`manifest.json.sig`, and the first boot (ODL-472) places both, byte-identical
to `release/`, before any seed exchange:

```caddyfile
	handle /manifest {
		header Cache-Control "no-store"
		reverse_proxy 127.0.0.1:8012
	}
	handle /manifest.sig {
		header Cache-Control "no-store"
		reverse_proxy 127.0.0.1:8012
	}
	handle {
		reverse_proxy 127.0.0.1:9120 {
			header_up Host {host}
			flush_interval -1
		}
	}
```

The manifest unit runs unprivileged and reads two public files. `/etc/odile` is
0750 root:odile-manifest and the listener is its only non-root reader:
`deploy/image/install-stack.sh` creates the group with `groupadd --system
odile-manifest` and the unit carries `SupplementaryGroups=odile-manifest` so a
`DynamicUser` can traverse the directory. `key.env` and `seed.json` inside stay
0600 root. `ExecStartPre` is the gate ODL-472 added: `odile-manifest.py --check`
decodes the release's base64 signature and verifies it over the exact manifest
bytes before anything listens, so a tampered pair makes the unit fail instead of
serving a document that looks signed. The listener after it is the release's
`manifest.py serve`, which sends the file's bytes without re-serialising them.

```ini
[Unit]
Description=Odile signed app-layer manifest
After=network.target
ConditionPathExists=/etc/odile/manifest.json
ConditionPathExists=/etc/odile/manifest.json.sig
ConditionPathExists=/etc/odile/odile-signing-key.pub
ConditionPathExists=/usr/local/lib/odile/manifest.py

[Service]
Type=simple
ExecStartPre=/usr/bin/python3 /usr/local/lib/odile/odile-manifest.py --check \
    --manifest /etc/odile/manifest.json \
    --signature /etc/odile/manifest.json.sig \
    --pubkey /etc/odile/odile-signing-key.pub
ExecStart=/usr/bin/python3 /usr/local/lib/odile/odile-manifest.py serve \
    --manifest /etc/odile/manifest.json \
    --signature /etc/odile/manifest.json.sig \
    --pubkey /etc/odile/odile-signing-key.pub \
    --program /usr/local/lib/odile/manifest.py \
    --bind 127.0.0.1 --port 8012
SupplementaryGroups=odile-manifest
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

The signed document and its signature are copied to `/etc/odile/` mode 0644 by
the first boot (ODL-472), byte-identical to `release/`; `/usr/local/lib/odile/manifest.py`
is the release's copy of this script and `odile-manifest.py` is the checker the
image build also copies beside it. A pair that does not verify never reaches a
listening socket: the unit exits, and `/manifest` answers nothing.

A machine that `odile_update.py apply` has moved to a newer pair keeps that pair
and the installer it names: the next boot keeps `/usr/local/bin/odile-install.sh`
only when its sha256 is the manifest's `installer_sha256`, and refuses the boot
when the file is missing or different. So the `sha256sum` in `CLAIMS.md` §4.6
still equals `manifest.installer_sha256` after an update and a reboot.

## 8. The tenant check (ODL-332)

On the tenant, after the install:

```sh
curl -sD- https://<slug>.odile.fyi/manifest | grep -i '^cache-control'
sha256sum /usr/local/bin/odile-install.sh        # must equal manifest.installer_sha256
git -C ~odile/.hermes/hermes-agent rev-parse HEAD # must equal manifest.hermes.commit
./scripts/manifest.py build --root / --user odile --out /tmp/tenant.json
```

The tenant build reads dpkg, so `packages.os` carries the guest's exact
versions rather than HQ's probe; it refuses a package without an installed
version. Compare the app-layer fields, not the whole document: `mode`,
`packages.source` and the version strings are build provenance; the installer,
commit, webapp, skill and package *sets* are the pinned layer.

## 9. Re-signing (HQ only)

```sh
$ ./scripts/manifest.py sign --manifest /tmp/manifest.json \
      --key /root/.odile/installer-signing.pem
```

`sign` refuses a key whose mode is not 0600, signs the file's exact bytes with
`openssl dgst -sign`, checks the result is 64 bytes and writes
`<manifest>.sig` as base64 (76 columns). To publish a new document, build it
from the release's installed root, sign it here, copy the two files to the
tenant and restart the unit. Rotating the key means a new
`installer/odile-signing-key.pub` and new recovery kits; because the URL reads
the deployed tree's copy on every request, publishing it is the file change
plus `./scripts/deploy.sh` — there is no second copy at the URL to update. An old
manifest verifies only with the key that signed it.

## 10. Boundaries and known gates

- **Operator signature.** Not hardware-measured, not a statement that the app
  is safe. CLAIMS.md gap 1 says it in public.
- **The audit hash names the script Odile ships.** ODL-208's
  `scripts/audit.py` is embedded in the installer and placed at
  `/usr/local/bin/odile-audit`; `build --audit /usr/local/bin/odile-audit`
  records it, and the release's `manifest.json` (`2026.11.12-4` onward) carries
  the same `audit_sha256` (`539e205f…`). The installer's own `--manifest` and
  its `--self-test` check the embedded copy against this checkout.
- **The apt install is pinned to the same table the manifest records.** The
  installer's `ensure_packages` passes `name=version` for every package from
  its `OS_PACKAGE_PINS`, resolved inside Debian 13 `trixie` at the dated
  snapshot in [`install.md`](install.md) §6.1; a pin missing from that snapshot
  stops the install before apt changes anything, with no fallback to the latest
  version. `--manifest` prints `os_snapshot`, `os_suite` and `os_packages`; the
  tenant's `packages.os` must equal those pins after the install, and that
  comparison is the release check (§8). The record and the command now say the
  same thing.
- **The public key URL is served from the deployed tree.** `/odile-signing-key.pub`
  returns the bytes of `installer/odile-signing-key.pub` on every request (200,
  `application/x-pem-file`, `Cache-Control: public, max-age=300`), so the
  recovery kit's copy and the URL cannot drift; a missing file is a 500, not a
  stale body. Rotation is §9: replace the file and deploy.
- **A scratch manifest says so.** `mode: scratch` and `packages.source: probe`
  mean HQ's toolchain stood in for the guest's dpkg. `restic` and `npm` are
  `null` and named in `packages.missing` on HQ because there is no
  `/usr/bin/restic` and no `/usr/bin/npm`; the tenant build reads dpkg and
  refuses a missing package instead.
- **The Webapp bundle must match the checkout.** `hermes-build.json`'s
  `pinnedCommit` and the checkout's `HEAD` are compared; a bundle built from
  another commit is an error, not a warning.

## 11. Evidence

Commands and output above were run in `<checkout>/.worktrees/t_dbaafa44` on
2026-11-08. The two manifests, their signatures and the served copies are in
`/root/.hermes/profiles/builder/cache/scratch/odl214-*-final.json*`; the raw
transcripts are `odl214-evidence-{1,2,3,4,5}.log` and the gate is
`odl214-make-check.log` in the same directory. Evidence 5 is the run at
f55d28a; the only later commit changes this pointer.

| DoD line | Observed |
|---|---|
| two fresh roots, same except timestamps | `diff <(sed … a) <(sed … b)` empty; `manifest.py diff` → `same (except generated_at)`; `sha256sum` 2f4c48cd… and de184be3… |
| the signature verifies with the published key | `signature: OK`, key `SHA256:ZyFEbkw2…`, exit 0; the kit's key is byte-identical to `installer/odile-signing-key.pub` |
| a changed file breaks the check | one skill line → `signature: BAD`, exit 3; restore → `2f4c48cd…` again |
| served at /manifest with no-store | `HTTP/1.0 200`, `Cache-Control: no-store`, body `cmp`-equal to the signed file, signature verifies over the served bytes |

`make check` on the branch: 522 tests OK (skipped=1), meter 12 OK, claims lint
14 files/14 phrases, brand lint 11 surfaces/17 colours.
