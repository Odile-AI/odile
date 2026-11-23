# Zero-touch first boot — the image installs itself (ODL-456)

This is the boot-to-page path of the strict flow: no terminal command, no SSH
key, no passphrase. The measured image carries a oneshot that exchanges the
one-time setup token for the tenant seed, runs the image's installer with it,
hands the sign-in hash in the tenant `.env` to Hermes basic auth, and brings up
the first-visit page. A second boot logs `same`; a missing or spent token logs
one fail-closed line and leaves the machine reachable.

The test evidence is the QEMU serial transcript, not this file. The commands
that produce it are in §5.

## 1. The channel: the measured command line

The only per-tenant input the sealed design has is the measured command line
(`docs/product/DECISION-agent-access.md` §3a). The first boot reads three
tokens from `/proc/cmdline`:

| Token | Meaning |
|---|---|
| `bb.iid=<id>` | Servury's instance id; the seed token is bound to it (ODL-455) |
| `odile.token=<64 hex>` | the one-time setup token, minted at provision |
| `odile.seed_url=<url>` | the seed endpoint (default `https://odile.fyi/seed`) |

`odile.token` and `odile.seed_url` are added by `sealed-image.sh` when
`ODILE_SEED_TOKEN` / `ODILE_SEED_URL` are set, or arrive verbatim in a
`--cmdline-file` line. A real tenant's line is composed at provision and the
disk is sealed to it, so the token is covered by the measurement.

**The trade-off, stated:** the command line is public. A host that reads the
panel's line can race the guest to `/seed` before the guest boots. The token is
single-use, bound to the instance, and expires in 24 hours; the exposure is the
machine's seed (the NEAR key and mail relay), not the disk. The alternative —
typing the token on the machine — is the user command the strict bar removes.
A host also cannot read the guest's memory or disk, so the seed is only exposed
if the host wins that boot-window race. This is an alpha.0 residual,
recorded here for the security seat.

## 2. The service

`odile-firstboot.service` is a oneshot, ordered after `network-online.target`
and before the tenant gateway. It runs
`/usr/local/lib/odile-image/odile-firstboot.py` and prints exactly **one**
`odile-firstboot:` line to the serial console (`StandardOutput=journal+console`):

```
odile-firstboot: installed odl-bench (instance …) · first-visit 200 · 4s
odile-firstboot: same
odile-firstboot: fail-closed: no setup token in the command line
odile-firstboot: fail-closed: seed token used
odile-firstboot: fail-closed: seed token expired
odile-firstboot: fail-closed: seed token is bound to another instance
odile-firstboot: fail-closed: seed token unknown
odile-firstboot: fail-closed: seed signature does not verify
odile-firstboot: fail-closed: seed service unreachable: …
odile-firstboot: fail-closed: installer failed (rc N)
odile-firstboot: fail-closed: first-visit page did not answer (…)
```

The program's order (each step writes only after the previous one passed):

1. place the release (ODL-472): the exact installer at
   `/usr/local/bin/odile-install.sh` (0755 root), the signed manifest pair at
   `/etc/odile/manifest.json`/`.sig`, and the listener that serves them, from
   the image's own copy of `release/`; the pair is verified before anything is
   written, and a second boot reads `same` for every byte in
   `/var/log/odile-firstboot.log`;
2. read the state at `/var/lib/odile/firstboot.json`; an installed machine
   with its seed cache still on disk prints `same` and exits — no exchange, no
   installer, no token spent;
3. without a token or an instance id, fail closed;
4. POST `{token, instance_id}` to the seed endpoint; **require** the
   `X-Odile-Signature: ed25519=…` signature over the exact response bytes.
   A seed carries its own key's public half (`signing_key`), the SHA-256 of
   those bytes (`signing_key_sha256`) and the release key's certificate over
   them (`signing_key_cert`, ODL-514): the certificate is verified against
   `/etc/odile/odile-signing-key.pub` (the release key that signs the
   installer) or the `odile-signing-key.prev.pub` beside it for the release
   that retires a key, then the body against the certified seed key. A seed
   issued before ODL-514 carries no certificate and is checked against the
   release key directly. A seed for another instance, or one that carries a
   credential-shaped field, is refused;
5. write `/etc/odile/seed.json` (0600, the exact signed bytes) and
   `/etc/odile/seed.sig` (0600, base64) — the cache pair the release installer
   reads (`installer/odile_install.py`, ODL-204) — then write
   `/etc/odile/first-visit.json` (0600) once: the token's SHA-256, never the
   token, and never rewritten, so a spent page stays spent;
6. run `/usr/local/lib/odile-image/install-stack.sh --seed … --first-visit-token …`;
   its output goes to `/var/log/odile-firstboot.log`, not the console. The
   installer converges the stack (`same` for everything the build did), hands
   the tenant `.env` (the sign-in hash Hermes reads) to the gateway, installs
   the first-visit service/page/unit and starts it;
7. assert the first-visit page answers through loopback (`/health` 200 and the
   form for this token 200), then write the state `ok` and print the line.

Fail-closed paths exit 0 on purpose: the unit is a oneshot, not a barrier. The
tenant gateway still starts (after the unit finishes), the Webapp gate answers,
and the machine is reachable. The state says `ok: false` with the reason, so a
later boot with a valid token (or the cache) retries.

**Retry from the cache.** If the exchange succeeds but the installer fails, the
seed and signature are already on disk; the next boot uses the cache and does
not touch `/seed` again (the token is spent by then). The token is required
only when there is no valid cache for this instance.

**A lost answer spends the token.** `seed.redeem` marks the token used before
it signs the response, so a timeout or a response this guest cannot verify
leaves no cache and the next boot logs `fail-closed: seed token used`. Rotate
the token (`odilectl provision`) rather than retrying the boot; only an
exchange that succeeded and an installer that failed retries from the cache.

## 3. The installer, and the first-visit page

`install-stack.sh` is the image's installer (`docs/runbooks/image-bench.md`
§1). With `--seed FILE --first-visit-token TOKEN` it adds:

- `/etc/odile/seed.json` 0600 and, once, `/etc/odile/first-visit.json` 0600;
- `first_visit.py` and `first-visit.html` (ODL-457) under
  `/usr/local/lib/odile/`, the unit, and the bench's socket-proxyd stand-in
  for Caddy on the first-visit port;
- `odile-first-visit.service` enabled, and started when PID 1 is present.

The page is a login (ODL-925): the installer writes the sign-in the seed
carries — the registration password's verifier — into the tenant `.env`
before the Webapp's first boot, so the POST verifies the password the user
chose at registration and shows the recovery code once. On a classic run with
no seed the page chooses the sign-in and writes its scrypt hash, as before.
Either way the POST restarts the gateway. On a tenant image the release
installer (`installer/odile_install.py --first-visit`, ODL-457) is the program
ODL-472 places at `/usr/local/bin/odile-install.sh` and ODL-332 runs; it
consumes the same cache pair, so the first boot's verified seed is not
exchanged twice, and it keeps the seeded verifier rather than writing a
placeholder.

**The release placement (ODL-472).** Step 1 is not a second installer: it
copies `release/`'s exact bytes — `odile_install.py`, `manifest.json`,
`manifest.json.sig`, the listener `manifest.py`, the checker `odile-manifest.py`
and the unit `odile-manifest.service` — and starts the loopback listener after
`odile-manifest.py --check` verifies the pair with `/etc/odile/odile-signing-key.pub`.
The image build copies that directory from `release/` and refuses to build if
the manifest does not name the installer it carries or either signature fails
(`sealed-image.sh`). The placement runs before the state short-circuit and
before the seed exchange, so an installed boot, a second boot and a fail-closed
boot all carry the release; a machine that already placed its own valid pair
keeps it together with the installer that pair names (the disk's installer is
kept when its hash is the manifest's `installer_sha256`; a pair whose installer
is missing or different fails closed), and a tampered pair fails closed with
`odile-firstboot: fail-closed: manifest signature does not verify`.

## 4. What the bench proves, and what only a Blackbox can prove

**The bench proves**: a signed UKI boot with the sealed disk, the token in the
measured line, the real `seed/seed.py` exchange and its Ed25519 signature, the
one-line console contract, the install applying the seed and enabling the page,
the first-visit page answering over the bench's Caddy stand-in, the release's
exact installer and signed manifest pair on the disk with `/manifest` serving
the signed bytes, a second boot printing `same` without an exchange, a tampered
pair failing closed with nothing listening, and an already-used token failing
closed while the machine stays reachable.

**Only a Blackbox can prove**: `SNP_GET_DERIVED_KEY` against a real VCEK and
PSP (the bench stand-in derives from the command line), the panel's command
line delivery, and a real tenant's DNS/ACME/certificate path. The bench's guest
`snpreport` is the ODL-454 stand-in; the seal/unseal semantics are unchanged.

## 5. The commands

The fixture is a real seed service with one tenant and one bound token; the
signing key is `/root/.odile/installer-signing.pem` (mode 0600) whose public
half is committed at `installer/odile-signing-key.pub`.

```sh
./deploy/image/firstboot-bench.sh prepare     # mint the token, one tenant (seed port 18081)
./deploy/image/firstboot-bench.sh build       # sealed disk + signed UKI; keeps a pre-boot disk copy
./deploy/image/firstboot-bench.sh boot --expect installed
./deploy/image/firstboot-bench.sh inspect-placement # 0755 root, byte-identical to release/, hash named by the manifest
./deploy/image/firstboot-bench.sh boot --expect same
./deploy/image/firstboot-bench.sh restore-fresh
./deploy/image/firstboot-bench.sh tamper-manifest   # one appended line; the signature no longer verifies
./deploy/image/firstboot-bench.sh boot --expect fail-closed
./deploy/image/firstboot-bench.sh stop
./deploy/image/firstboot-bench.sh cleanup
```

The fixture seed service listens on 18081, not the live service's 8081.
The fixture directory (default `/var/tmp/odile-456-bench`) holds the token
(0600), the seed database, `fresh.raw` and the boot transcripts. `boot` runs
`deploy/image/bench.py sealed` with `--expect-firstboot`, which reads the one
line from the boot serial (ttyS0), fetches the page through the forwarded
first-visit port, and then runs the five image checks unchanged (the
first-boot line and the page are extra evidence, not a sixth check).

To reproduce by hand, the same program is a CLI:

```sh
/usr/bin/python3 deploy/image/odile-firstboot.py --print-state   # secret-free
/usr/bin/python3 deploy/image/odile-firstboot.py --check         # 0 = installed
```

## 6. The final run (ODL-456 evidence)

The evidence was produced on the merged tree `3c6b913` (main `e1ff48c`,
ODL-457 merged) with a fresh fixture token and `build --force`; the pin in
this directory is that build. The boot serials are the transcripts; the lines
below are quoted from them verbatim (`[ time] python3[pid]:` is systemd's
journal passthrough).

First boot, `boot --expect installed` — the whole first-boot path in one line:

    [   95.444703] python3[634]: odile-firstboot: installed odl-bench (instance 45445445-0000-4000-8000-000000000454) · first-visit 200 · 24s

and the two extra checks from the same boot, beside the unchanged smoke set:

    ODILE-BENCH first-visit PASS GET /first-visit -> 200 (form: yes, 37352 bytes)
    ODILE-BENCH final PASS image-bench: PASS (5/5)
    sealed-bench: PASS (unlocked without a passphrase; 5/5 image checks; installed odl-bench …; GET /first-visit -> 200 (form: yes, 37352 bytes))

Second boot, `boot --expect same` — no exchange, no installer, token untouched:

    [   70.977182] python3[621]: odile-firstboot: same

Fail-closed, `restore-fresh` then `boot --expect fail-closed` — the first boot
spent the token and the disk is back at its as-built state:

    [   69.839816] python3[634]: odile-firstboot: fail-closed: seed token used

That boot still passed the five image checks (`image-bench: PASS (5/5)`): the
gate answered and the Webapp logged in, so a fail-closed machine is reachable.

The installed disk, opened read-only after the second boot (`bench-snpreport
--unseal` with the pinned line, `cryptsetup luksOpen`, `mount -o ro`):

    /etc/odile/seed.json          0600  501 B  sha256 c696b7cc…
    /etc/odile/seed.sig           0600   89 B  base64 Ed25519, verifies
    /etc/odile/first-visit.json   0600  token_sha256 = 607a820a…, used_at null
    /usr/local/lib/odile/first_visit.py     0755  32472 B
    /usr/local/lib/odile/first-visit.html   0644  40050 B
    multi-user.target.wants/odile-first-visit.service -> enabled
    grep of /etc/odile for the token plaintext: none

The pin's `raw_image_sha256` is the as-built `fresh.raw` (611d493e…); the
signed UKI is b0c97050…, measurement a6536581…, and `sbverify --cert
deploy/image/odile-image-signing.crt` says `Signature verification OK`.

## 7. Rollback

The unit changes nothing until the seed verifies. A machine that fails closed
is a machine with the stock stack and no tenant; deleting
`/var/lib/odile/firstboot.json` makes the next boot retry from the seed cache
(no token needed), and deleting the cache as well makes it exchange again — which
needs a rotated token, because the old one is spent.
