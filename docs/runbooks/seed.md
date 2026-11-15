# Seed service — one-time token → signed tenant config

**Status:** v0, built by ODL-204 · **Service:** `seed/seed.py` · **Route:** `POST https://odile.fyi/seed`
**Consumers:** the signed installer (ODL-203); `odilectl rotate-token` (M2)

A freshly installed machine knows two things: the setup token its owner received
in the "ready" email, and its own Servury instance id. It exchanges both at
`/seed` for the tenant config. The token works once, dies after 24 hours, and is
bound to the tenant *and* the instance id, so a leaked token cannot seed a
different machine. HQ stores only a SHA-256 hash of the token; no tenant login
credential exists at HQ or in transit.

```
installer ──POST /seed {token, instance_id}──▶ Caddy :443 ──▶ seed/seed.py :8081 ──▶ SQLite
     ◀── 200 seed JSON + X-Odile-Signature: ed25519=… ────────────────────────────┘
     verifies the signature against the Odile public key in the recovery kit,
     writes /etc/odile/seed.json (0600), then installs
```

TLS is Caddy's job, as it is for the waitlist. The service itself binds
`127.0.0.1` and speaks plain HTTP.

## 1. The exchange contract

Request — `POST /seed`, `Content-Type: application/json`, body at most 4 KiB:

```json
{"token": "<64 lowercase hex>", "instance_id": "<servury instance id>"}
```

The token travels in the **body**, never in the URL, so no proxy access log can
capture it. The service keeps no access log of its own.

Success — `200`, `Cache-Control: no-store`, and the signed body:

```json
{
  "slug": "odl-7f3a2c",
  "instance_id": "srv-…",
  "near_api_key": "…",
  "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
  "support_public_key": "ssh-ed25519 AAAA… support@odile.fyi",
  "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/odl-7f3a2c",
  "mail_relay": {"host": "smtp.mail.odile.fyi", "port": 587,
                 "username": "odl-7f3a2c@mail.odile.fyi", "secret": "…"},
  "hermes_version": "2026.11.1"
}
```

These eight fields are the v0 contract; the test suite pins the set. `{slug}` in
any config string is substituted for the tenant's slug. The body contains **no
user login credential**: no SSH private key, no passphrase, no Webapp password,
no LUKS material, and not even the user's public key or email address. The
inference and mail-relay values are machine API credentials the installer writes
into local config, not credentials that open the machine.

Refusals are JSON `{"error": "...", "reason": "..."}`:

| Status | `reason` | Meaning | What to do |
|---|---|---|---|
| 400 | `bad_request` | body is not JSON, empty, or has the wrong shape | fix the installer request |
| 400 | `bad_token` / `bad_instance` | malformed token or instance id | copy both values exactly |
| 403 | `wrong_instance` | the token belongs to another machine | rotate a token for this instance |
| 404 | `unknown_token` | no such hash | typo or token from an older rotation; rotate |
| 410 | `used` | already exchanged (or rotated away) | use the cached seed, or rotate |
| 410 | `expired` | 24 hours have passed | rotate |
| 413 | `too_large` | body over 4 KiB | send only the two fields |
| 500 | `internal` | signing key or config broken | `journalctl -u odile-seed`; the detail is on stderr |
| 502/curl 7 | — | Caddy route or service down | `systemctl status odile-seed`, then the Caddy block in §3 |

A refused request never burns the token: the conditional `UPDATE` that consumes
it runs only after every check passes. Two simultaneous exchanges produce one
`200` and one `410` — covered by `test_concurrent_redemption_yields_exactly_one_seed`.

## 2. Token lifecycle

- 32 random bytes, hex-encoded (64 characters), minted by `secrets.token_hex`.
- Stored as `sha256(token)` in `seed_tokens`; the plaintext exists only in the
  command output that minted it (and in the ready email).
- One use; `used_at` is set by the exchange. `rotate` also sets `used_at` on
  every live token, which is how "ended" appears in `token list`.
- 24-hour expiry, `CHECK (expires_at > created_at)` in the schema.
- Bound to `tenant_id` **and** `instance_id`; the exchange compares the
  instance id before consuming the token.
- Rotate after a rebuild, a suspected leak, or an expiry. The ready email
  carries the token — never a password, passphrase or private key.

Mint, inspect, rotate (run as the `odile` user or root; the DB is the same one
`app.py` uses):

```sh
/usr/bin/python3 /opt/odile/seed/seed.py token issue  --tenant odl-7f3a2c
/usr/bin/python3 /opt/odile/seed/seed.py token list   --tenant odl-7f3a2c
/usr/bin/python3 /opt/odile/seed/seed.py token rotate --tenant odl-7f3a2c --dry-run
/usr/bin/python3 /opt/odile/seed/seed.py token rotate --tenant odl-7f3a2c
```

`issue` prints the token once, last line, and a note on stderr:

```
tenant:   odl-7f3a2c
instance: srv-…
expires:  2026-11-07T12:00:00Z
token:    <64 hex>
```

`issue` refuses a tenant with no `server_id` unless `--instance` is given — a
token that is not bound to an instance is not a seed token. `token list` prints
only the first 8 hash characters:

```
hash8     created              expires              state    instance
d116232c  2026-11-06T12:00:00Z 2026-11-07T12:00:00Z ended    srv-…
7718abf3  2026-11-06T12:00:00Z 2026-11-07T12:00:00Z live     srv-…
```

`rotate --dry-run` prints how many live tokens would end and changes nothing.

### Where the ledger lives

`seed_tokens` is an additive table created idempotently by `seed.py`, in the
same SQLite file as `tenants` (`/var/lib/odile/waitlist.db`). It is separate
from `claims` on purpose: a claim token opens the claim page, a setup token
seeds a machine, and neither may be redeemed as the other. Schema v2's
`tenants.setup_token_hash` is a single hash with no expiry or `used_at`, so it
cannot express single-use plus expiry; it stays reserved for the invite-stage
token. If a later card consolidates the two, move the ledger — do not fork it.

## 3. HQ setup (one time)

Run as root on `odile.fyi`. Steps 1–2 (the keys and the seed config) are the
one-time prep the deploy assumes. Steps 3–4 (the unit and the route) are
committed in `deploy/seed/` and applied by `./scripts/deploy.sh`; [`deploy.md`](deploy.md)
§3 is the deploy section, and the commands here remain the manual fallback and
the record of what the deploy does.

1. **Two keys, one chain** (Ed25519; ODL-514). The key that signs seeds is no
   longer the key that signs releases:

   | Key | Where it lives | What it signs |
   |---|---|---|
   | release key | CI-held; never on this box | the installer, `manifest.json`, `release.json`, and the seed certificate |
   | seed key | here, `/root/.odile/seed-signing.pem` (0600, root) | every tenant seed |

   The release public half is `installer/odile-signing-key.pub`; the key being
   retired ships beside it as `installer/odile-signing-key.prev.pub` for one
   release. The seed public half is `deploy/seed/seed-key.pub`, and
   `deploy/seed/seed-key.crt` is the release key's Ed25519 signature over that
   public key's bytes. Both are public and committed.

   **The seed key** is created by the deploy, once, when it is absent —
   `deploy/seed/deploy_seed.py` calls `ensure_seed_key`, writes the key 0600
   root and its public half to `deploy/seed/seed-key.pub`. It never replaces a
   key that exists. By hand, the same thing:

   ```sh
   /usr/bin/python3 /opt/odile/seed/seed.py keygen --out /root/.odile/seed-signing.pem
   # key:         /root/.odile/seed-signing.pem (0600)
   # public:      /root/.odile/seed-signing.pem.pub (0644)
   # fingerprint: sha256:…
   ```

   **The certificate** is cut in the release workflow (ODL-517) by
   `make seed-cert`, signed with the release key the CI signing backend holds. A
   freshly generated seed key has no certificate until then, and the deploy
   refuses to install the service on a key whose certificate does not verify
   (`error: deploy/seed/seed-key.crt does not verify against
   installer/odile-signing-key.pub`). That refusal is the safety property: a
   service that signs seeds nobody can verify is worse than a service that does
   not start.

   The release key is CI-held and never lands on this box (ODL-515 adds the
   signer seam, ODL-517 wires it). It is an **operator signature — provenance,
   not a hardware measurement** (CLAIMS §3, gap 1).

### 3a. The signing ceremony

Signing runs in the release workflow (ODL-517); no release key lives on this
box and no human signs a release by hand. The key is held by the CI signing
backend — the backend is chosen on `t_54b8f354`; Security recommends Cloud KMS
Ed25519 via OIDC, with a non-exportable key. ODL-515 adds the `SIGNER=` seam
and re-cuts this paragraph when the backend lands; ODL-517 runs it. The signing
order in the workflow is:

```sh
make release-sign FILE=manifest.json
make release-sign FILE=release.json
make release-sign FILE=installer/odile_install.py
make seed-cert    FILE=deploy/seed/seed-key.pub
make pin-sign     # ODL-515: the measurement pin
```

`release-sign` signs one file; `seed-cert` certifies the seed public key. The
targets take two forms of signer:

- `KEY=<path>` — a file key, the form a hand-run or a test uses. A `KEY=` that
  resolves under `/root` is refused: *the key that signs releases never lives
  on the HQ box*.
- `SIGNER=<command>` — the CI form, added by ODL-515. The release workflow
  passes the backend's signer command here, so the key is never a file on any
  machine. Until ODL-515 lands, `KEY=` is the only implemented form.

Each `release-sign` prints the file's `sha256sum` and the base64 signature
*before* it writes `<FILE>.sig`, and verifies it against
`installer/odile-signing-key.pub` (override with `PUBKEY=` while a new key is
being cut). `seed-cert` writes the 64-byte certificate to
`deploy/seed/seed-key.crt` and copies the certified public key to
`deploy/seed/seed-key.pub`; a second run over the same key produces identical
bytes (Ed25519 is deterministic).

The workflow commits the result: `*.sig`, `deploy/seed/seed-key.crt` and
`deploy/seed/seed-key.pub`. Then deploy, which checks the chain before it
starts the service:

```sh
./scripts/deploy.sh
```

**Moving from the file key to the CI key** is the same ceremony, one file
different: the old public half moves to
`installer/odile-signing-key.prev.pub`, the new one is written to
`installer/odile-signing-key.pub`, and the sequence above runs through the CI
signer. Seeds served during the cut keep verifying because the first boot and
the installer both accept a certificate signed by the current key **or** the
retiring one, and the installer prints which one verified. `.prev.pub` ships for one release and is then
removed. The seed key itself only changes if the seed certificate is re-cut — a
new seed key without a new certificate is a deploy refusal, not a silent break.

The recovery kit carries the certificate beside both public halves — the
release key and the seed key — each labelled, so the kit stays an anchor
whichever key signed.

Verification, by hand, at any point:

```sh
$ openssl dgst -verify installer/odile-signing-key.pub \
      -signature deploy/seed/seed-key.crt deploy/seed/seed-key.pub
Verified OK
$ sha256sum deploy/seed/seed-key.pub deploy/seed/seed-key.crt
```

2. **Seed config** (root-only). The inference key and the relay credential come
   from the HQ secret store (`/root/.odile/`, mode 0600 — AGENTS.md). `{slug}`
   is substituted per tenant.

   ```sh
   umask 077
   cat > /root/.odile/seed-config.json <<'JSON'
   {
     "near_api_key": "…",
     "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
     "support_public_key": "ssh-ed25519 AAAA… support@odile.fyi",
     "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/{slug}",
     "hermes_version": "2026.11.1",
     "mail_relay": {"host": "smtp.mail.odile.fyi", "port": 587,
                    "username": "{slug}@mail.odile.fyi", "secret": "…"}
   }
   JSON
   chmod 600 /root/.odile/seed-config.json
   ```

   The service refuses to start on a config or key that is group- or
   world-readable, so a bad `umask` fails loudly instead of leaking the NEAR key.

3. **Unit** — `/etc/systemd/system/odile-seed.service`, committed at
   `deploy/seed/odile-seed.service`; `./scripts/deploy.sh` installs it. It runs as `odile`
   because it parses internet input; `LoadCredential=` hands it the two
   root-owned files without a copy in a home directory.

   ```ini
   [Unit]
   Description=Odile seed service (one-time token -> signed tenant config)
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=odile
   Group=odile
   WorkingDirectory=/opt/odile
   StateDirectory=odile
   StateDirectoryMode=0700
   Environment=ODILE_DB=/var/lib/odile/waitlist.db
   Environment=ODILE_SEED_BIND=127.0.0.1
   Environment=ODILE_SEED_PORT=8081
   Environment=PYTHONUNBUFFERED=1
   LoadCredential=seed-config:/root/.odile/seed-config.json
   LoadCredential=seed-key:/root/.odile/seed-signing.pem
   ExecStart=/usr/bin/python3 /opt/odile/seed/seed.py serve
   Restart=always
   RestartSec=2
   NoNewPrivileges=yes
   ProtectSystem=strict
   ProtectHome=yes
   PrivateTmp=yes
   RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
   RestrictNamespaces=yes
   LockPersonality=yes
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   ```

   ```sh
   systemctl daemon-reload
   systemctl enable --now odile-seed
   curl -fsS http://127.0.0.1:8081/health     # ok
   ```

   Run it as `odile`, the same user as `app.py`: both open the same SQLite
   file and its WAL sidecars, and two different users on one WAL database is a
   permission bug waiting for the first concurrent write.

   `LoadCredential=` is what lets an unprivileged service use the seed key:
   `/root` is 0700, so `odile` cannot read `/root/.odile/seed-signing.pem`
   itself. Do **not** add `Environment=ODILE_SEED_SIGNING_KEY=…` to the unit —
   the variable wins over the credential, and the service would fail on a file
   it cannot read. Keep the variable for a hand-run `serve` as root (and for the
   rehearsal in §5), where it names the same key:

   ```sh
   ODILE_SEED_SIGNING_KEY=/root/.odile/seed-signing.pem \
     /usr/bin/python3 /opt/odile/seed/seed.py serve
   ```

   The service loads one key and one certificate: the seed key above and
   `deploy/seed/seed-key.crt` (from the tree it runs in). It verifies the
   certificate against `installer/odile-signing-key.pub` at start and refuses
   to boot when the pair does not hang together, printing the ceremony step in
   the error. The release key is not loaded by this unit at all.

4. **Caddy route** — committed at `deploy/seed/odile-seed.caddy`; `./scripts/deploy.sh`
   splices it between `# BEGIN odile-seed` and `# END odile-seed`.
   `admin off`, so a change is a restart. In the
   `odile.fyi` site block, replace the bare `reverse_proxy 127.0.0.1:8080`
   with two handlers so `/seed` and only `/seed` reaches the seed service;
   keep `header` and `log` as they are.

   ```caddyfile
   odile.fyi {
       encode zstd gzip

       header {
           Server odile
           Strict-Transport-Security "max-age=31536000; includeSubDomains"
       }

       log {
           output discard
       }

       @seed path /seed
       handle @seed {
           reverse_proxy 127.0.0.1:8081
       }

       handle {
           reverse_proxy 127.0.0.1:8080
       }
   }
   ```

   ```sh
   caddy validate --config /etc/caddy/Caddyfile
   systemctl restart caddy
   curl -s -o /dev/null -w 'GET /seed -> %{http_code}\n' https://odile.fyi/seed   # 405
   curl -s -o /dev/null -w 'GET /     -> %{http_code}\n' https://odile.fyi/        # 200
   ```

   `GET /seed` is 405 by design: the exchange is a POST so the token never
   lands in a URL.

## 4. Installer side (the contract ODL-203 implements)

The installer holds `TOKEN` (from the ready email) and `INSTANCE_ID` (the
machine's own Servury id, readable from the metadata service). It verifies the
response before parsing it, and caches the verified bytes so a re-run is
idempotent.

```sh
umask 077
curl -sS --fail-with-body -X POST https://odile.fyi/seed \
  -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\",\"instance_id\":\"$INSTANCE_ID\"}" \
  -D /run/odile-seed.headers -o /run/odile-seed.json

SIG=$(awk 'tolower($1) == "x-odile-signature:" { sub(/^ed25519=/, "", $2); print $2 }' \
        /run/odile-seed.headers | tr -d '\r')
printf '%s' "$SIG" | base64 -d > /run/odile-seed.sig

# 1. the certificate inside the body: the release key's signature over the seed key
/usr/bin/python3 - <<'PY'
import base64, json
seed = json.load(open("/run/odile-seed.json"))
open("/run/odile-seed-key.pub", "w").write(seed["signing_key"])
open("/run/odile-seed-key.crt", "wb").write(base64.b64decode(seed["signing_key_cert"]))
PY
openssl dgst -verify odile-signing-key.pub \
  -signature /run/odile-seed-key.crt /run/odile-seed-key.pub
# Verified OK

# 2. the body: the certified seed key
openssl dgst -verify /run/odile-seed-key.pub \
  -signature /run/odile-seed.sig /run/odile-seed.json
# Verified OK

install -m 0600 /run/odile-seed.json /etc/odile/seed.json
rm -f /run/odile-seed.headers /run/odile-seed.json /run/odile-seed.sig \
      /run/odile-seed-key.pub /run/odile-seed-key.crt
```

`odile-signing-key.pub` is the file shipped beside the installer; it is the
kit's `signing_public_key` and the key that signed the installer itself. The
seed body is signed by the *seed* key (a different key), which the body carries
together with `signing_key_sha256` (the SHA-256 of that public key's file bytes)
and `signing_key_cert` (the release key's signature over them). The installer
walks that chain and prints the release key that certified the seed key. A seed
issued before ODL-514 carries none of the three and is checked against the
release key directly — the old shape still verifies. (The installer writes no
copy under `/etc/odile` — it holds the kit's PEM for the run and caches only
the seed and its signature.)

`seed.py verify` is the same check with a friendlier exit code:

```sh
/usr/bin/python3 /opt/odile/seed/seed.py verify \
  --pubkey odile-signing-key.pub --sig "$SIG" --body /etc/odile/seed.json
# signature: OK (certified by …/odile-signing-key.pub)   (exit 0)
# signature: BAD (...) and exit 1 otherwise
```

Rules the installer must keep:

- **Verify before parse, every run**, including a cached `/etc/odile/seed.json`.
  The signature covers the exact bytes; a one-byte edit fails.
- **Cache at `/etc/odile/seed.json`, mode 0600.** A second run reads the cache
  and skips the exchange; the token is already spent. `--refresh` means mint a
  new token with `rotate`, then exchange.
- **Never print the seed.** It carries the NEAR key and the mail relay secret.
- The signature is Odile's operator key, not a hardware measurement. Say so if
  the installer prints provenance.

## 5. Rehearse the four exchanges

This is the whole DoD on a throwaway database, with the real service, the real
signature check and the real refusals. The rehearsal mints a throwaway seed key
*and* a throwaway release key, because the service refuses to start unless the
release key certifies the seed key (ODL-514): at HQ the seed key lives at
`/root/.odile/seed-signing.pem` and the release key is CI-held and never lands
on this machine. `ODILE_SEED_SIGNING_KEY`, `ODILE_SEED_CERT` and
`ODILE_SEED_RELEASE_PUBKEY` are how a hand-run service names them. Run it from
the repo root; it needs `/usr/bin/python3`, `openssl`, `curl` and a free port
(18081 here).

```sh
set -eu
T=$(mktemp -d)
export ODILE_DB="$T/waitlist.db"
export ODILE_SEED_CONFIG="$T/seed-config.json"
export ODILE_SEED_SIGNING_KEY="$T/seed-signing.pem"
export ODILE_SEED_CERT="$T/seed-key.crt"
export ODILE_SEED_RELEASE_PUBKEY="$T/release-signing.pub"
export ODILE_SEED_PORT=18081
export ODILE_SEED_BIND=127.0.0.1
umask 077

/usr/bin/python3 seed/seed.py keygen
# the release key, and its certificate over the seed public key (ODL-514)
openssl genpkey -algorithm ED25519 -out "$T/release-signing.pem"
openssl pkey -in "$T/release-signing.pem" -pubout -out "$T/release-signing.pub"
openssl dgst -sign "$T/release-signing.pem" -out "$ODILE_SEED_CERT" \
  "$ODILE_SEED_SIGNING_KEY.pub"
chmod 644 "$ODILE_SEED_CERT"
cat > "$ODILE_SEED_CONFIG" <<'JSON'
{
  "near_api_key": "near-rehearsal-key",
  "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
  "support_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIREHEARSAL support@odile.fyi",
  "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/{slug}",
  "hermes_version": "2026.11.1",
  "mail_relay": {"host": "smtp.mail.odile.fyi", "port": 587,
                 "username": "{slug}@mail.odile.fyi", "secret": "rehearsal-relay"}
}
JSON
/usr/bin/python3 seed/seed.py init
/usr/bin/python3 - "$ODILE_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
for slug, server in (("odl-alpha", "srv-alpha"), ("odl-beta", "srv-beta")):
    con.execute("INSERT INTO tenants (slug, email, server_id, created_at) VALUES (?, ?, ?, ?)",
                (slug, slug + "@example.com", server, "2026-11-06T12:00:00Z"))
con.commit()
PY
/usr/bin/python3 seed/seed.py serve > "$T/serve.log" 2>&1 &
SERVE_PID=$!
trap 'kill $SERVE_PID 2>/dev/null || true; wait $SERVE_PID 2>/dev/null || true; rm -rf "$T"' EXIT
for _ in $(seq 1 100); do
  curl -fsS "http://127.0.0.1:$ODILE_SEED_PORT/health" >/dev/null 2>&1 && break
  sleep 0.05
done

ask() {  # ask <token> <instance> <outfile> -> "<code> <body>"
  code=$(curl -sS -o "$3" -D "$3.headers" -w '%{http_code}' \
    -X POST "http://127.0.0.1:$ODILE_SEED_PORT/seed" \
    -H 'Content-Type: application/json' \
    -d "{\"token\":\"$1\",\"instance_id\":\"$2\"}")
  printf '%s %s\n' "$code" "$(cat "$3")"
}

TOKEN=$(/usr/bin/python3 seed/seed.py token issue --tenant odl-alpha | awk '/^token:/{print $2}')
echo "1. valid        -> $(ask "$TOKEN" srv-alpha "$T/valid.json")"
echo "2. replay       -> $(ask "$TOKEN" srv-alpha "$T/replay.json")"

EXPIRED=$(/usr/bin/python3 seed/seed.py token issue --tenant odl-alpha | awk '/^token:/{print $2}')
/usr/bin/python3 - "$ODILE_DB" "$EXPIRED" <<'PY'
import hashlib, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("UPDATE seed_tokens SET created_at = '2026-11-04T12:00:00Z',"
            " expires_at = '2026-11-05T12:00:00Z' WHERE token_hash = ?",
            (hashlib.sha256(sys.argv[2].encode()).hexdigest(),))
con.commit()
PY
echo "3. expired      -> $(ask "$EXPIRED" srv-alpha "$T/expired.json")"

OTHER=$(/usr/bin/python3 seed/seed.py token issue --tenant odl-beta | awk '/^token:/{print $2}')
echo "4. wrong tenant -> $(ask "$OTHER" srv-alpha "$T/wrong.json")"

SIG=$(awk 'tolower($1) == "x-odile-signature:" { sub(/^ed25519=/, "", $2); print $2 }' \
        "$T/valid.json.headers" | tr -d '\r')
/usr/bin/python3 seed/seed.py verify --pubkey "$ODILE_SEED_RELEASE_PUBKEY" \
  --sig "$SIG" --body "$T/valid.json"
# signature: OK (certified by …/release-signing.pub)
printf '%s' "$SIG" | base64 -d > "$T/valid.sig"
# the body is signed by the seed key the certificate names
/usr/bin/python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); open(sys.argv[2],"w").write(s["signing_key"])' \
  "$T/valid.json" "$T/seed.pub"
openssl dgst -verify "$T/seed.pub" -signature "$T/valid.sig" "$T/valid.json"
# Verified OK
```

Expected shapes, all observed on the build box:

```
1. valid        -> 200 {"backup_repo_url":"sftp:…/odl-alpha",…,"slug":"odl-alpha"}
2. replay       -> 410 {"error":"token already used","reason":"used"}
3. expired      -> 410 {"error":"token expired","reason":"expired"}
4. wrong tenant -> 403 {"error":"token is bound to another instance","reason":"wrong_instance"}
signature: OK (certified by …/release-signing.pub)
Verified OK
```

Also worth checking after a rehearsal (never in production output):

```sh
/usr/bin/python3 - "$ODILE_DB" "$TOKEN" "$T/valid.json" <<'PY'
import hashlib, json, sqlite3, sys
seed = json.load(open(sys.argv[3]))
con = sqlite3.connect(sys.argv[1])
stored = con.execute("SELECT token_hash FROM seed_tokens WHERE token_hash = ?",
                     (hashlib.sha256(sys.argv[2].encode()).hexdigest(),)).fetchone()
raw = json.dumps(seed).lower()
print("stored as sha256:", stored is not None)
print("fields:", sorted(seed))
print("no user key:", all(k not in raw
      for k in ("password", "passphrase", "private key", "luks")))
PY
```

## 6. Failure modes and recovery

| Symptom | Likely cause | Fix |
|---|---|---|
| `410 used` on the first attempt | the installer retried or reused an old token | use the cached `/etc/odile/seed.json`; otherwise `rotate` and re-email |
| `410 used` on a **reinstall** of an image already built | the token baked into that image's command line was spent by its first boot | un-end that row (§6a) — `rotate` mints a plaintext the sealed command line does not carry |
| `410 expired` | more than 24 h between email and install | `rotate`; the ready email's token is dead by design |
| `403 wrong_instance` | token minted for another instance (rebuild, swap) | `rotate --instance <new id>` |
| `404 unknown_token` | typo, or a token from before a rotation | `token list --tenant <slug>`; rotate |
| `502` through Caddy | seed service down or route missing | `systemctl status odile-seed`; `journalctl -u odile-seed`; re-check §3.4 |
| service exits at boot, `chmod 600` in the log | config or key is group/world-readable | `chmod 600` the file named in the message |
| service exits at boot, `field … missing` | incomplete config | fix `seed-config.json`; the message names the field, never its value |
| `500 internal` | OpenSSL missing, key unreadable, DB locked | the journal carries the detail; the client gets no secret |

A refused exchange never consumes the token, so the fix is usually "correct the
request and retry"; only `used`/`expired` need a rotation.

### 6a. Reinstalling a machine re-presents a spent token

`odile-firstboot` reads `odile.token=` from the measured command line and
exchanges it once per fresh install. A machine rebuilt from the same image wipes
`/etc/odile/seed.json`, so it exchanges again — with the same token, because the
plaintext is baked into the image it has just booted. The ledger marked that
token used at the first boot, so the second exchange answers `410 used`. The
release installer says so and offers the one instruction that cannot work here:

> error: seed exchange refused with 410 (used) and no cached seed exists.
> The setup token is single-use; ask Odile for a new one, or restore
> `/etc/odile/seed.json` and `/etc/odile/seed.sig` from the first run.

A token minted by `rotate` goes nowhere near a command line already sealed into
the image, so "a new one" is not available to a built image. `odile-firstboot`
prints `odile-firstboot: fail-closed: seed token used` on the serial console and
exits **0** by design (fail-closed is "not installed", never "failed boot"). The
release-setup run behind it installs nothing and logs `=== rc=1` — and writes its
done marker anyway, carrying `rc=1`, so every later boot prints
`already converged` and the failure is sticky: `--force` is the only re-run
([`demo-public.md`](demo-public.md) §6). The box comes up with the tenant's own
Caddy block only and serves nothing.

The repair is to un-end the row of the token that image carries, **before** the
reinstall and in the same maintenance window:

```sh
# 1. a copy you can put back. The database is WAL, so a plain cp of the .db
#    alone can miss rows that are committed but still only in the -wal
sudo sqlite3 /var/lib/odile/waitlist.db \
  ".backup '/root/.odile/waitlist.db.bak-$(date -u +%Y%m%dT%H%M%SZ)'"

# 2. the token the image carries, and its row's hash (only the hash is stored)
objcopy -O binary --only-section=.cmdline odile-tenant.efi /tmp/cmdline
TOKEN=$(tr -d '\0' < /tmp/cmdline | tr ' ' '\n' | sed -n 's/^odile.token=//p')
HASH=$(printf '%s' "$TOKEN" | sha256sum | awk '{print $1}')

# 3. clear used_at and push the expiry out; the only rules are later than
#    created_at (the table's CHECK) and later than the install
sudo -u odile sqlite3 /var/lib/odile/waitlist.db \
  "update seed_tokens set used_at = NULL,
     expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+30 days')
   where token_hash = '$HASH';"

# 4. read it back: no used_at, the new expiry
sudo -u odile sqlite3 /var/lib/odile/waitlist.db \
  "select substr(token_hash,1,8), used_at, expires_at
     from seed_tokens where token_hash = '$HASH';"
```

`token list --tenant <slug>` shows the same row `live` again. Without the image
in hand the blunt form names the instance instead of the hash:

```sh
sudo -u odile sqlite3 /var/lib/odile/waitlist.db \
  "update seed_tokens set used_at = NULL,
     expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+30 days')
   where instance_id = '<servury id>';"
```

That one un-ends **every** token the instance ever held, including the ones
`rotate` and `bind` ended. Each still redeems only this tenant's own seed (§7),
so it is not a cross-tenant hole — it is a wider set of plaintexts that can
replay the seed, some of them in old ready emails. Name the hash when you can.

What the reset does and does not move:

- **Copy with `.backup`, not `cp`.** Measured 2026-11-15 on this schema: a row
  a live writer had committed was in `/var/lib/odile/waitlist.db` and absent
  from a plain `cp` of that one file, because WAL keeps it in the `-wal` until
  the next checkpoint; `sqlite3 .backup` carried it. A `cp` is not a backup of
  this database.
- **Run the update as `odile`**, the user that owns `/var/lib/odile` (0700) and
  runs the service. Root writes it too — SQLite gives the `-wal`/`-shm` it
  creates the database file's owner, measured after a `kill -9` — but `odile`
  is the one user the state directory is meant for.
- **Reset and reinstall in one window.** While `used_at` is NULL the
  *still-running* machine's boot-record POST is refused `409 not_redeemed`
  (`record_for_token` requires a spent token). The **log** line says so — on a
  converged boot the console is exactly `odile-firstboot: same` — the machine
  keeps serving, and the reinstall's own exchange spends the row again.
- **An expired row does not break a running machine, but it silences the boot
  record.** The exchange installs once; the same token is also the bearer for
  the boot-record POST on every boot, and `record_for_token` answers an expired
  row `410 expired`. That is non-fatal (the console still says `same`), but the
  ledger's boot record (`boot_release`, `recorded_at`, `first_visit`) stops
  refreshing. Nothing on the box changes until the next reinstall, and a reset
  re-arms the record along with the token.
- **A replay hands over machine credentials.** The seed carries the NEAR key and
  the mail relay secret, and a tenant provisioned as a rebuild also gets its
  `rebuild` block, sealed under this same token (`/var/lib/odile/seed-rebuilds.json`,
  the restic repository password). The reset re-arms a machine's credentials,
  not just its install: whoever holds that image holds the token.

A machine booted from an image **built with someone else's `bb.iid`** answers
`403 wrong_instance` only while that token is unspent; once the token has been
spent, the exchange answers `410 used` first (`redeem` checks `used_at` before
`instance_id`). The exchange posts the `bb.iid` baked into the image, not the
hardware's id, so staging the same image on a new Servury instance is still this
case — the reset follows the id in the command line (or the token hash), never
the new server id. A rebuild mints a fresh token bound to the new id
(`odilectl provision`, or `token rotate --tenant <slug> --instance <new id>`)
and builds a new line carrying it.

## 7. Security notes

- **No tenant login credential at HQ.** The ledger holds a hash; the seed holds
  API credentials for inference and mail, plus a *public* support key. No SSH
  private key, passphrase, Webapp password or LUKS key exists here.
- **Token at rest:** SHA-256 of 32 random bytes. A database leak does not yield
  a usable token.
- **Token in transit:** in the POST body over TLS; never a URL, never a query
  string, never logged by this service (no access log), never echoed in a
  response.
- **Binding:** tenant + instance id. A token copied from another tenant's email
  fails with 403 and does not burn.
- **Signing:** Ed25519 over the exact response bytes, from the *seed* key
  `/root/.odile/seed-signing.pem` (0600) — not the release key — handed to the
  unprivileged service via `LoadCredential=`. Each seed carries the seed public
  key, `signing_key_sha256` over its bytes, and `signing_key_cert`, the release
  key's signature over those bytes. The installer verifies the certificate
  against the release public key in the recovery kit (or the key being retired,
  during a rotation) and the body against the certified seed key; it prints
  which key verified. The signature proves the bytes came from Odile, not from
  the host or the network; the certificate is what binds the seed key to the
  release key without putting the release key on this box.
- **Two keys, one chain:** the uid that serves the site can read the seed key
  through the unit's credential directory, and that is all it can read. The
  release key is CI-held; `make release-sign` and `make seed-cert` refuse a
  `KEY=` under `/root`, and the release workflow signs through `SIGNER=`
  (ODL-515) so the key is never a file on this box.
- **File modes enforced:** config and key 0600 (group/world bits refuse boot);
  `/etc/odile/seed.json` 0600; the service binds loopback only.
- **Rotation:** `token rotate` ends every live token for the tenant in one
  transaction, then mints one new token. Do it on rebuild, leak, or expiry.

## 8. Rollback and destroy

The service is additive: no schema is dropped, no tenant row is touched.

```sh
systemctl disable --now odile-seed
```

Then edit `/etc/caddy/Caddyfile`. The managed region is
`# BEGIN odile-seed` … `# END odile-seed`, and it carries both routes: deleting
it whole would leave the site with no handler at all. Delete the matcher and the
seed handler, and keep the app's catch-all:

```caddyfile
	handle {
		reverse_proxy 127.0.0.1:8080
	}
```

```sh
systemctl restart caddy
# /seed now falls through to app.py, which answers 404; tokens are inert.
```

`token rotate` invalidates live tokens without stopping anything. Deleting a
tenant's rows is `DELETE FROM seed_tokens WHERE tenant_id = ?` (the table has no
append-only trigger; a token row is operational state, not the event log).

## 9. Handoffs

- **ODL-203 (installer)** implements §4 and needs `odile-signing-key.pub` in the
  release bundle. That PEM is the kit's `signing_public_key`; the seed key is a
  second key, and the kit carries both halves plus the certificate that binds
  them (ODL-514). The certificate check is the one place the installer must not
  improvise.
- **M2 `odilectl rotate-token`** maps to `seed/seed.py token rotate`; `--dry-run`
  is already there.
- **The public signing key is served** at
  `https://odile.fyi/odile-signing-key.pub` (CLAIMS §4.6): `app.py` reads
  `installer/odile-signing-key.pub` from the deployed tree on every request
  (200, `application/x-pem-file`, `Cache-Control: public, max-age=300`), so the
  PEM a stranger fetches is the kit's `signing_public_key`. Writing its
  fingerprint into the recovery file is still later wiring.
- **Not in v0:** per-tenant NEAR keys (one org key with `{slug}` substitution),
  email delivery of the token, and a web form. The token is handed over by the
  ready email; the seed is fetched by the installer.
