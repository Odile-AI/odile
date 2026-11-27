# ODL-466 — Access claims verdict: the strict bar against the tenant evidence

**Seat:** security · **Card:** ODL-466 · **Decided text:** `docs/product/CLAIMS-RECUT-STRICT.md` (ODL-461, 2026-11-10), not the v3.7/v3.8 deck
**Evidence base:** `main` `8c2ad99` (ODL-332 merged) · tenant `odl-fake01` · `203.0.113.87` · Servury Blackbox Plus `0badc0de`
**Gate:** `make acceptance CARD=ODL-466` at `main` `898d493` (ODL-208 + ODL-335 + the UKI-cycle merge); the gate lines are in §10 and the full output is pasted in the handoff.
**Live checks:** 2026-11-12T17:31Z – 23:13Z UTC (every timestamp below is UTC). ODL-217's grant drill ran 21:09:48Z – 21:24:49Z and restored the guest at 21:39:57Z / 22:00:08Z; the 23:12Z re-read is the post-drill handoff state (§1 A10, §4.7).
**Scope:** A1–A11, the audience and first-run rows (§3), §4.1–§4.11, the §2 forbidden-table replacement, every §5 re-cut (the §1 rows, the FAQ answers, the privacy-inventory row), and gaps 1–3. A cut row still gets a verdict (it must be consistent with the evidence).

**Retired mechanism, 14 November 2026 (card t_7f339817).** The meter rows in
this verdict — the `odile-meter` user, the demo/tenant meter key, the
meter-capped demo — are the 2026-11-12 record. The meter was removed from the
product code at `f884184` (merged `0a4b726`); a tenant's inference credential is
`/etc/odile/key.env` (one key per user) and the model route is NEAR AI direct.
No verdict row is rewritten.

**How to read a verdict.** `PASS` means the evidence proves the decided sentence for a tenant and the command behind it is in this file. `BLOCK` means the sentence must not land: either the proof has not run, or the sentence is false against the machine. A block names the missing proof and the card that would produce it — never a reworded sentence. A page is never the proof: the machine's own report file is read, or the command behind it is run. No row is reworded here; the words are the product lead's.

**The two findings that decide the most rows** are §0.1 (the disk key is still the build key) and §0.2 (the published image carries a usable pre-first-visit sign-in and the live session secret). Both were reproduced with the commands in §9; §0.1 was in the 19:27Z draft, §0.2 is new.

**Addendum, 13 November 2026 (`t_8a0ef011`, ODL-319 follow-up).** The §4.4 optional command is now PASS, and the §7 row below is closed. The published bundle `https://odile.fyi/odile-verify.tar.gz` (sha256 `d200329260d66ce4ec545b8d34e05b262c06111ee506449c6c44958937250b53`, built with `mtime=0` so the hash moves only with the reviewed files) was run on `odl-fake01` at 13 November 2026: it fetched the tenant's own VCEK from AMD's KDS for chip `3fa18b…` and TCB `bl=4 tee=0 snp=23 ucode=213` (`vcek PASS`), chained it to the pinned ARK (`chain PASS`), bound it to the report (`cpu binding PASS`), verified the signature (`signature PASS`), read policy `0x30000`, checked the committed TCB (`tcb PASS committed snp=28 >= 27`), took the tenant release from the served document (`release PASS 2026.11.2 is pinned`), matched the ready-email number (`expected PASS 9f01725a…f137`) and exited 0. A byte changed at `0x90` fails the signature and exits 1; the demo `./odile-verify https://odile.fyi` still passes with no argument (measurement `e99728ef…3fbc`, exit 0). The independent `sev-snp-measure` over the published UKI (`odile-tenant.efi` `5cb31808…` + OVMF `8731846c…`, 4 vCPU, `EPYC-v4`, guest features `0x1`) reproduces `9f01725a…f137`, and the KDS VCEK chain walk prints `Verify OK`. §4.4's command now carries the tarball hash and `--expect` (`docs/product/CLAIMS-RECUT-STRICT.md` §4.4; the runbook is `docs/runbooks/verify.md` §"A tenant run, from a laptop"). The row below keeps the 12 November finding as history.

**Bundle, 19 November 2026 (`t_3cd62ef9`, ODL-906-F3 follow-up).** The tree now builds a different bundle, because the verifier no longer vendors a chip's VCEK: it ships AMD's ASK and ARK and fetches the report's own VCEK from the KDS on every run. That bundle was sha256 `815dbc26c8a584d0271620445009d8242466223d0ad8d6ff71ed21fd95208d4c` (20,560 B, `mtime=0`). The `d2003292…0b53` hash above is the 13 November artifact and stays as history; no public sentence changed, and the fixtures are now synthetic (see `docs/security/LEAK-CONTROL-VERDICT.md`, follow-up section).

**Bundle, 20 November 2026 (`t_a37c001d`).** The pin now names the demo's 2-vCPU plan (UKI `47be81ca…`, measurement `83bd3fce…`; `docs/runbooks/verify.md` §"The 2-vCPU plan"), so the current bundle is sha256 `99395155ae1d08e67f81e3a64096c918300992700d8fe8ccd3f8b16762073bde` (20,562 B, `mtime=0`). The pin is one of the five reviewed files in the archive, so its content change moves the hash. No public sentence changed; `docs/CLAIMS.md` §4.4's command block still carried the 19 November hash at this record's date — the Product Lead's edit, the `t_a37c001d` follow-up card `t_b0cc9015`, re-cut it on 21 November 2026, and the deck now names this bundle.

---

## 0. Findings that block rows

### 0.1 The disk key is not sealed, and Odile holds it

The seal in `bb-sealed-unlock`/`bb-sealed-provision` is `SNP_GET_DERIVED_KEY(root_key_select=VCEK, guest_field_select=POLICY|MEASUREMENT)`. It seals **a keyslot key** — a fresh 64-byte passphrase `V'` — into LUKS keyslot 1. It does **not** re-key the LUKS volume.

`bb-sealed-provision` adds `V'` with `cryptsetup luksAddKey` and kills keyslot 0 with `luksKillSlot` (`deploy/image/bb-sealed-provision:111-141`). Both operations preserve the LUKS **master (volume) key**. The published disk image carries keyslot 0 — the as-built passphrase `V`, also published on the image's boot partition as `/odile/luks-bootstrap.key` — and that keyslot wraps the same master key. So the master key is recoverable from the **publicly downloadable** disk image with the **published** bootstrap key, and the live tenant's disk still uses it.

Proof, on a header backup of the published image and a 64 MiB sparse device (the master key never printed; only its SHA-256; fresh run 21:17:19Z, `/var/tmp/odile-466-run/masterkey-test-2116.log`):

```
$ cryptsetup --batch-mode luksDump --dump-master-key --key-file luks-bootstrap.key <published>/dev/sda3
MK dump: <64 bytes>                      # sha256 74f2552b29882e057cca64622dadd261e992384b784fefa53d4d34745bd64505
$ cryptsetup luksAddKey   --key-file luks-bootstrap.key --key-slot 1 <header-copy> newkey   # bb-sealed-provision
$ cryptsetup luksKillSlot --key-file newkey <header-copy> 0                                 # bb-sealed-provision
$ cryptsetup --batch-mode luksDump --dump-master-key --key-file newkey <header-copy>
MK dump: <64 bytes>                      # sha256 74f2552b… — identical; "master key unchanged: YES"
$ cryptsetup open --readonly --volume-key-file <master-from-published-image> <header-copy> odile466mk
open with the published-image master key after key-slot 0 was killed: OK
$ cryptsetup luksOpen --test-passphrase --key-file luks-bootstrap.key <header-copy>
open with the bootstrap key after key-slot 0 was killed: refused (the keyslot is gone)
```

The published image itself opens with its published bootstrap key — mounted read-only at 21:18:59Z (`/var/tmp/odile-466-run/image-open-test-2118.log`):

```
== boot partition: the published bootstrap key
-rw------- 1 root root 64  /run/…/odile/luks-bootstrap.key
== the root partition opens with that key (read-only)
open: OK
== keyslots on the published image
Keyslots:
  0: luks2   (aes-xts-plain64, argon2id)
Tokens:
```

The tenant's first boot did exactly the two keyslot operations — `docs/evidence/m1-tenant.md` §9.8: `killed the previous keyslot 0`, `sealed: … (keyslot 1)`, `shredded the first-boot bootstrap key`. The volume key is unchanged. `/var/lib/odile/seal.json` on the live machine (read 21:04:29Z) confirms `{"sealed":true,"keyslot":1,"bootstrap_removed":true,"at":"2026-11-12T14:44:18Z"}`. The published image is still served: `HEAD https://odile.fyi/releases/m1/odile-tenant-sealed.raw` → `200`, `content-length: 8589934592`, `cache-control: no-store` (20:57Z); its SHA-256 is `c1278242…` (the panel's Disk image, §4 of `docs/evidence/m1-tenant.md`).

Therefore:

- **Odile** (which built and serves the image) can open the tenant's disk offline. So can **Servury** (which holds the disk bytes) and anyone else who obtains them.
- The seal is an **additional** keyslot, not the only door. A changed boot image that carries the recovered master key opens the disk without the measurement — the seal-break drill's "changed image stays locked" is the *honest-image* case only.
- The master key is the **build-time** key; the guest never generates it. "No one at Odile holds it", "the disk unlocks only on the boot you verify", "the host sees ciphertext", and "we cannot read it" are all false until the volume is re-keyed at first boot.

**The smallest change that clears this cluster:** at the first boot, re-key the volume with a **guest-generated master key** — `cryptsetup reencrypt` with a new volume key (i.e. without `--keep-key`), or a first-boot `luksFormat` with a guest-generated volume key and the installed template copied in — then seal that key. `luksAddKey` + `luksKillSlot` alone is not a re-key. **Card: `t_28dda3b7` — ODL-454 follow-up** (`deploy/image/bb-sealed-provision`, `deploy/image/sealed-image.sh`, `tests/test_sealed_image.py`, `docs/runbooks/image.md`). ODL-454 is done and its hook is correct; no existing card owned the re-key. The public copy is the product lead's; this verdict blocks the proof, not the words.

This is the one row that is not a wording problem: the product's defining property — the user is the only keyholder — is not true of the live tenant until the re-key.

### 0.2 The sign-in gate has a public default before the first visit, and the session secret is the image's

The published image carries `/home/tenant/.hermes/.env` with exactly three keys and a marker:

```
# Odile image bench — generated by deploy/image/install-stack.sh, never edited
# by hand. Exactly three keys: the judge-facing login and its signing secret.
# No provider key: the bench runs no inference.
# bench-password-sha256: 47f758a4ad12daa47780a9d887c02a7a415324d58220856c85fa8d393e4ddaed
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=tenant
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=<86 chars>      # scrypt of odile-bench-not-a-secret
HERMES_DASHBOARD_BASIC_AUTH_SECRET=<64 chars>              # the session-signing key
```

I verified the hash: it accepts `odile-bench-not-a-secret` (the default at `deploy/image/install-stack.sh:47`), scrypt n=16384 r=8 p=1, salt 16 B, dk 32 B — `bench password matches the image hash: True` (21:57Z, `bench-hash-check-2157.log`). The first boot runs `install-stack.sh --seed … --first-visit-token …`, then the release installer with `--first-visit`; `ensure_env` keeps an existing hash — the "placeholder for a discarded random password" branch is skipped when the image already has a hash (`installer/odile_install.py` `def ensure_env` `:10049`, keep-hash branch `:10067`, hash read `:10093` at `main` `898d493`; `:9003`/`:9021`/`:9046` at the evidence base `8c2ad99`). So **from the first boot until the owner completes the first visit, `tenant` / `odile-bench-not-a-secret` opens the workspace.** On the live tenant that window was 14:44:45Z (first-visit page 200) → 14:57:46Z (first visit) — 13m01s. The URL is in the ready email and in Certificate Transparency logs.

The same `.env` carries the session-signing secret, and it is the **live** secret: a token minted with the image's secret and sent with the login's binding cookie returns `GET /api/sessions 200` on the tenant (without the binding cookie: 401 — the `hermes_webapp_signin` browser-credential binding is what stops a bare forger; see `/var/tmp/odile-466-run/forge_test.py`, 21:23Z). `installer/first_visit.py:130` preserves the secret, so every tenant from the image shares it. The image also ships `/etc/ssh/ssh_host_{ed25519,rsa,ecdsa}_key` (0600 root): the live tenant presents the image's ed25519 host key `SHA256:IRqReY12…`, so anyone with the image can impersonate a tenant's SSH host. SSH is not in the user path (the rows that named a host key are cut), but it is a private key in a public artifact.

**The smallest change that clears this:** the image ships **no usable password hash** (a hash of a discarded random value, or none with the provider still registered), `ensure_env(first_visit=True)` always replaces an existing hash, the signing secret is generated **per tenant at first boot**, and the image ships **no SSH host private key** (first boot runs `ssh-keygen -A`). Test on a fresh boot: bench login 401 before the first visit, the user's password 200 after, two fresh boots present different host keys. **Card: `t_adcdec31` — ODL-456 follow-up.** This blocks A1, A7, §4.2's "only sign-in is yours" clause and the §5 sign-in answer until it lands.

---

## 1. A1–A11

| # | Decided row (verbatim) | Verdict | Evidence (link + timestamp) |
|---|---|---|---|
| A1 | "No one at Odile can log in to a tenant machine. The only sign-in is yours." | **BLOCK** (the operator half PASS; the "only sign-in is yours" clause blocked) | Operator half PASS: the machine's own access report at 15:07:06Z (read 17:51:27Z) and a **fresh** report at 21:24:00Z (read 21:24), both five PASS lines, incl. `usable keys PASS 0 usable user keys`, `operator key PASS none (authorized_keys: 1 line(s), the measured throwaway)`; `ssh -F /dev/null -o BatchMode=yes root@203.0.113.87 true` → `Permission denied (publickey)`, exit 255, 21:39Z; live `/etc/passwd` has no operator account (only `tenant`, `odile-meter` nologin, system users); `sshd_config.d/99-odile.conf` `PasswordAuthentication no` (read 17:51:27Z). **Block:** the image's bench hash is a public sign-in until the first visit (§0.2; window 14:44:45–14:57:46Z), and the session secret is the image's. **Missing proof:** a fresh boot where the bench password is refused before the first visit. **Card: `t_adcdec31`.** |
| A2 | "The boot key in the command line is a public key with no private half. It is plumbing, not a credential." | **PASS** | The pin's `bb.key` decodes to `ssh-ed25519 … odile-bench throwaway boot key`, `SHA256:I2VaMwa+RRbbA6/f4AJhQUJv3WhAnjQvNxqmWZAVPCM`, `boot_key_private_half: shredded` (`/var/tmp/odile-332-prod11/tenant-image.pin`); the build generates and `shred -u`s the private key (`deploy/image/sealed-image.sh:253-258`); the published image's `/root/.ssh/authorized_keys` holds exactly that throwaway public key (read from the image 21:18Z), and no private half of the boot key is in the image, git, the build logs or on disk (a tree grep finds no private boot key; the `BEGIN OPENSSH PRIVATE KEY` matches are prose, a `[REDACTED PRIVATE KEY]` placeholder and test fixtures). Live `/proc/cmdline` (669 B) carries the `bb.key=` token; `/verify` `boot key PASS … public key only, no private half of it on the machine` (21:24:00Z). |
| A3 | "The disk unlocks only on the boot you verify. Change the image, and it stays locked." | **BLOCK** | The seal is bound to `policy\|measurement` and the bench variant-UKI fails closed (m1-image.md; the tenant's own `unseal rc 6` branch, serial §9.8). But the master key is unchanged (§0.1), so a changed image carrying that key opens the disk regardless of the measurement. **Missing proof:** a first-boot re-key. **Card: `t_28dda3b7` (ODL-454 follow-up).** |
| A4 | "There is no passphrase to type. The disk key is sealed to the measured boot, and no one at Odile holds it." | **BLOCK** | No-passphrase half PASS: 0 `Please unlock disk` prompts in the round-8 serial; `bb-sealed-unlock: root_crypt opened with the SNP-derived key` after the 15:05:12Z restart; `/verify` `seal PASS policy 0x30000; seal_id 53ac131d…; measurement 9f01725a…` (21:24:00Z). Disk-key half BLOCK: the seal wraps a keyslot key, not the master key; the master key is recoverable from the published image (§0.1; MK SHA-256 `74f2552b…` unchanged after the first boot's operations). **Missing proof:** first-boot re-key. **Card: `t_28dda3b7`.** |
| A5 | "After a reboot, it starts itself. The seal is the key." | **PASS** | Restart 15:05:12Z → `/attestation` new `fetched_at` 15:07:05Z, same measurement; `/` 302 → `/login?next=%2F` at 15:09:36Z; serial `bb-sealed-unlock: root_crypt opened with the SNP-derived key`, `odile-firstboot: same` (m1-tenant.md §9.8/§10). Re-observed on ODL-217's reboot 20:43:45Z (`fetched_at` 20:43:45Z) and 21:24:00Z (`fetched_at` 21:24:00Z, same `9f01725a…`), `/verify` 200 at 21:24. The seal is *a* key on the normal path; the master-key caveat is A3/A4. |
| A6 | "The machine waits for its owner. This is not downtime; it is the lock." — **cut** | **PASS (cut)** | Nothing to prove; the evidence is A5: the machine did not wait (15:05 restart, 302 at 15:09; no unlock prompt). Keeping it would contradict A5. |
| A7 | "Your Odile sign-in is set on your machine at first visit and hashed there. We never see it. It is the only app-layer gate: no 2FA in V0.1." | **BLOCK** | Set-at-first-visit half PASS: first visit 14:57:46Z (form 200, POST 200, recovery code once) — m1-tenant.md §9.8/§10; `/verify` `sign-in hash PASS scrypt hash present in /home/tenant/.hermes/.env; no plaintext password` (21:24:00Z); the first-visit service runs on the guest behind Caddy; HQ's `tenants` schema has no password column (21:50Z). Blocked: before the first visit the gate accepts the image's public bench password, and the session secret is the image's (shared, public — §0.2). **Missing proof:** a fresh boot where the gate is shut until the first visit. **Card: `t_adcdec31`.** |
| A8 | "At HQ we hold your email, the record of your machine, and a boot key with no private half. Not the disk key, not your sign-in." | **BLOCK** | Record half PASS: `tenants` holds email/slug/server_id/ipv4/plan/state/ssh_public_key/setup_token_hash; `tenant_state` the host key; no private-key, passphrase or password column (21:50Z). "A boot key with no private half" PASS (no private half at HQ). "Not the disk key" BLOCK: the disk master key is recoverable from the published image (§0.1). The row also still holds the pre-strict owner key `IMdWy…`, not the measured throwaway — that is §4.11's block. **Missing proof:** first-boot re-key. **Card: `t_28dda3b7`.** |
| A9 | "A recovery code and a recovery file are the way back: the code resets your sign-in, the file opens your backup. Your first visit gives you both; keep them off the machine. While the machine is still running, the agent can show it again." | **BLOCK (backup clause); the rest PASS** | Code half PASS: reset drill 15:11:44Z — reset 200 + new code, new sign-in 200 + 4 `__Host-` cookies + session 200, old sign-in 401, used code refused (`That recovery code does not match.`) — m1-tenant.md §10.2. File half PASS: done page carries the row verbatim and its download link; the 605 B file's `Certificate SHA-256: sha256:f903497d…` = the served cert `F9:03:49:7D…:F4:DB` (14:57Z); the file is 0600 root (`recovery-seed.py`; installer `DEFAULT_RECOVERY`), so the agent fallback is scoped to the running machine. "The file opens your backup" is unproven: no backup exists yet. **Missing proof:** a restore into a rebuilt guest with the recovery file. **Card: ODL-215 (M2, running); the sentence lands with ODL-474.** |
| A10 | "A support grant is a time-boxed door into the running machine. It ends for new logins when it expires or you revoke it." | **PASS** (drill committed; lands with ODL-217's merge) | ODL-217's committed drill (`odl/ODL-217` `e69dd7f`, `docs/runbooks/support.md` §8; raw transcripts attached to `t_5bd24b98`): a 1-minute grant (`a3d388506a8b`, `SHA256:0GGWIg…`) let root in through the forced command and logged `event=start`/`event=end` at 21:09:48Z; expiry at 21:10:59Z removed the key line (`grep -c` 0 at 21:11:08Z) and a new login was refused (255, `Permission denied (publickey)`); a 24-hour grant (`ced8c0d4689b`) was revoked at 21:12:17Z with an open `pts/1` session — the line went immediately (new login 255) and `pkill -t pts/1` ended the session (rc=143, `event=end` 21:12:22Z, `signal=SIGTERM`); `status` then said `no grant (expired …)` / `no grant (revoked …)` with the tombstones kept. The drill measured the card's reboot premise false — the boot-time installer rewrites `authorized_keys` to one line — so `expire` re-asserts a live grant from the state within 30 s (`event=reassert` 21:15:02Z and 21:24:49Z, the second measured before/after a reboot); expiry and revoke are the closures. Guest restored to the installer state 21:39:57Z / 22:00:08Z (`restore-final.txt`: 1 boot-key line, 0 odile-support lines, no program/timer/state); live `/verify` at 23:12Z is five PASS on the 21:27:15Z access report. **Condition:** ODL-217 is in round 2 (docs/evidence fixes only; its verifier's own live smoke of the reviewed bytes `3f710593…` passed); if the branch head changes, the drill re-runs, and the row lands with that card's merge. |
| A11 | "A repeating job — the morning brief, a watchdog — runs when the machine comes back up if it was due while it was down." | **PASS** | The 2-minute `bench-heartbeat` log, read live 18:04:43Z: `15:02:50Z`, `15:04:50Z`, then **one** line `15:07:54Z` for the 3m04s gap across the 15:05:12Z restart; the log continues normally after (`15:09:56Z` … `18:03:48Z`). A second instance across ODL-217's 20:43:45Z reboot: `20:42:31Z`, then **one** line `20:44:43Z` (read 21:04:29Z). One catch-up run, not one per missed interval (m1-tenant.md §10.2). The row is narrowed to repeating jobs; the one-shot >120 s exclusion is not exercised and is not claimed. |

---

## 2. The audience and the first run (§3)

| Old row | Decision | Verdict | Evidence |
|---|---|---|---|
| "Technical enough to generate one SSH key" (positioning) → "For people who want an agent, not a server." | accept | **PASS** | Positioning, not a machine proof; the browser-only path below is what makes it true. |
| (same row) → "You need a browser. Not a terminal." | accept | **PASS** | Every required step is a browser step on the tenant: claim page, first visit (form 200 / POST 200, 14:57:46Z), recovery code and file download, `/verify` (200, 17:31Z, 20:41Z, 21:24Z), reset form (15:11Z); the agent terminal and the laptop verifier are optional. The support answers are "ask the agent" (§5). |
| "Claiming takes 3 minutes. Then about 20 minutes of setup…" → "Claiming takes 3 minutes. Then the machine builds itself: you check the report, set your sign-in, and start." | change | **BLOCK (the "3 minutes" number untimed); the machine half PASS** | Machine half: panel save 14:42:53Z → first-visit 200 at 14:44:45Z (112 s), release half done 14:46:43Z (2m50s), m1-tenant.md §9.8/§10; `/verify` 200 at 21:24. The "3 minutes" is the claim page's number and was never clocked: ODL-459's page probe (`/var/tmp/odile-odl459-round3/probe.json`, 2026-11-10) records layout and tab order, not timing, and BRAND forbids unmeasured numbers. **Missing proof:** a clocked claim-page submission (open → email+consent → confirm) on the live page. **Card: ODL-462** — carry the clocked run, or the product lead drops the number (the words are the product lead's). The page order (check-then-set) is noted, not blocked: `/verify` is public and available from boot; ODL-462's claim page carries the order. |
| "The passphrase is typed over SSH and stays on the machine." | cut | **PASS (cut)** | No passphrase; no SSH in the user's path. |
| "The disk is encrypted with LUKS2, with a passphrase you set at first boot." → "The disk is encrypted with LUKS2; its key is sealed to the measured boot." | change | **BLOCK** | LUKS2 + `aes-xts-plain64` proven (published image `luksDump`, 21:18Z). "Its key is sealed" is the master-key defect (§0.1): a keyslot key is sealed, not the disk key. **Missing proof:** first-boot re-key. **Card: `t_28dda3b7`.** |
| "Check the machine before you trust it: the verifier prints PASS or FAIL, and you type the passphrase only after PASS." → "Check the machine before you trust it: the verify page shows PASS or FAIL, and you set your sign-in only after PASS." | change | **PASS** | `/verify` shows the five PASS lines and the `read from your machine` tag (21:24:00Z report; page 200 at 21:24). The page does not *enforce* check-then-set; the instruction is true and ODL-462's claim page carries the order. |

---

## 3. §4 — the evidence section

| § | Heading (decided) | Verdict | Evidence (link + timestamp) |
|---|---|---|---|
| 4.1 | "Zero usable user keys." | **PASS** | Live access report 15:07:06Z (read 17:51:27Z), re-generated by the machine at 21:24:00Z (read 21:24): `0 usable user keys; every authorized_keys line is the measured boot key (SHA256:I2VaMwa…)`; the published image's `/root/.ssh/authorized_keys` holds exactly that throwaway (read from the image 21:18Z); no tenant `authorized_keys`. The 21:18Z FAIL was ODL-217's live grant drill (A10), not a handoff state. |
| 4.2 | "Servury's panel has no guest agent and cannot reset root. There is no operator account; the only sign-in is yours, unless you grant support (§4.7)." | **BLOCK (the "only sign-in is yours" clause); the operator half PASS** | Live `sshd_config.d/99-odile.conf`: `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password` (17:51:27Z); image `/etc/shadow` root `*`, `tenant` `!` (read from the image 21:50Z); the installer locks root (`passwd -S root`/`passwd -l root`, `installer/odile_install.py:9233-9240`); no operator account in live `/etc/passwd` (17:51Z); `ssh` 255 (21:39Z). The live `passwd -S root` was not captured (the file API 403s `/etc/shadow`); the image + installer are the proof, and no one has root except the user (the grant is the exception §4.7 describes). **Block:** the "only sign-in is yours" clause is §0.2. **Card: `t_adcdec31`.** |
| 4.3 | "No human keyslot; the seal." … "Odile never had a key or a passphrase." | **BLOCK** | The live seal half: `/var/lib/odile/seal.json` keyslot 1, bootstrap_removed true (21:04:29Z); no tokens in the published header (`luksDump`, 21:18Z). "Odile never had a key" is **false**: Odile generated the as-built keyslot passphrase `V` and published it on the image's boot partition as `/odile/luks-bootstrap.key`; the master key is the build-time key (§0.1), and the image opens with that key (21:18:59Z). **Missing proof:** first-boot re-key. **Card: `t_28dda3b7`.** |
| 4.4 | "The boot was measured, and you checked it before you signed in." (the optional laptop verifier bundle) | **PASS (the row); BLOCK (the optional command)** | Row PASS: `/verify` five PASS lines + measurement 9f01725a…; `/attestation` 200, measurement = the pin, `fetched_at` 21:24:00Z; the ready-email template carries `Measurement {{MEASUREMENT}}` (`templates/emails/ready.txt`; `tests/test_claim_page.py` binds it). **Optional command BLOCK:** the re-cut's command `./odile-verify https://<slug>.odile.fyi` from `https://odile.fyi/odile-verify.tar.gz` (re-downloaded 21:49Z, sha256 `cbd6b854da4ef8aa7f7a1f0c13c5b038ada57c5863fb3b8ff590729292b6289b`) run on the tenant at 21:50Z: `cpu binding FAIL  the VCEK is for chip 967acf…, the report names 3fa18b…`, `signature FAIL  the report signature does not verify with the VCEK`, `release FAIL  release not pinned: 2026.11.2`, exit 1 (the chain, policy, TCB and report lines PASS separately). **Missing proof:** a verifier that fetches the tenant's own VCEK (AMD KDS), pins/carries the tenant release, and takes `--expect`. **Card: `t_8a0ef011` (ODL-319 follow-up)** — or ODL-462 drops the optional command, which the product lead decides. |
| 4.5 | "This is a genuine SEV-SNP guest." | **PASS (the claim); BLOCK (the command block)** | Claim PASS: the tenant report (raw 1,184 B, version 5) verified with the VCEK fetched from AMD's KDS for the report's own chip `3fa18b…`: `chain PASS` (VCEK→ASK→ARK, ARK SHA-256 `69:d0:63…`), `cpu binding PASS`, `signature PASS`, `policy PASS 0x30000` (DEBUG and MIGRATE_MA clear), `tcb PASS committed snp=28 ≥ 27`, `report PASS`; measurement 9f01725a… = my `sev-snp-measure --kernel` of the published UKI (`/var/tmp/odile-466-run/verify-report-1952.log`). **Command block BLOCK:** the re-cut's `--kernel vmlinuz --initrd initrd.img --append '<cmdline>'` reproduces `cdcfc9f8…` (loose) — **not** the tenant's `9f01725a…`; the UKI form `sev-snp-measure --mode snp --ovmf OVMF.amdsev.fd --kernel odile-tenant.efi --vcpus 4 --vcpu-type EPYC-v4 --guest-features 0x1` reproduces `9f01725a…`. **Missing proof:** the v3.9 evidence block for a UKI tenant; **card: ODL-462** (the command, not a claim sentence; the builder's `measurements.txt` has the exact command). |
| 4.6 | "The installer and app manifest" | **PASS** | Live installer 554,867 B, sha256 `4aee80c5…` (read 17:51:27Z) = `/manifest`'s `installer_sha256` (`/manifest` 200, 49,981 B, sha256 `1ed9dcad…` = `release/manifest.json`, re-checked 21:39Z); `/manifest.sig` 200, 90 B, `e99d1f28…`; the path is placed 0755 root (`odile-firstboot.py` `place_release`, `0o755`). The placement is zero-touch; the signed pair matches. |
| 4.7 | "Support access is off." (accepted unchanged) | **PASS** | The drill installed the program and live grants in each leg (expiry, revoke, two reboot runs), then restored the installer-shipped state: `restore-final.txt` at 21:39:57Z and 22:00:08Z shows `1 /root/.ssh/authorized_keys` (the measured boot key), `0` odile-support lines, no `/var/lib/odile/support-grant.json`, no `/usr/local/sbin/odile-support`, `0 timers`, and `odile-support: command not found`; `status` returned `no grant` / the expiry and revoke tombstones; the live access report 21:27:15Z (read 23:12Z) is five PASS incl. `operator key PASS none (authorized_keys: 1 line(s), the measured throwaway)`. The installer placement of the program is ODL-474; "support access is off" is true either way. |
| 4.8 | "The host sees ciphertext." (accepted unchanged) | **BLOCK** | Memory half PASS: the report's policy 0x30000 has DEBUG clear; the report chain is genuine. Disk half BLOCK: the host holds the disk bytes and the master key is recoverable from the published image + bootstrap key (§0.1). **Missing proof:** first-boot re-key. **Card: `t_28dda3b7`.** |
| 4.9 | "No trackers." (accepted unchanged) | **PASS** | `curl -sI https://odile.fyi/` → `content-security-policy: default-src 'none'; …` (21:57Z); no third-party requests; the public pages carry no script. |
| 4.10 | "The brief is signed and sent by Odile's own mail server." (accepted unchanged) | **PASS** | Gmail headers `spf=pass dkim=pass dmarc=pass`, placement INBOX — `docs/evidence/m0-bench.md:21,75` (2026-11-11T13:12:14Z), `docs/evidence/odl-238/`; the ready template carries the measurement. The strict bar changes no mail path; ODL-332 did not re-run a ready/brief send, noted. |
| 4.11 | "HQ stores the throwaway public key, a hash of your setup token and the row that is your machine; there is no private half and no passphrase, and the sign-in hash is on the machine." | **BLOCK (the first clause)** | No private half / no passphrase / sign-in hash on the machine PASS: live `.env` has only the Hermes keys (the `.env` itself is 403 to the file API as a sensitive path; the root-run report reads it); `/api/sessions` 401 anonymous, 200 with the session (21:24Z). The first clause is **false for this tenant**: `tenants.ssh_public_key` is the pre-strict owner key `ssh-ed25519 …IMdWyPF1…` (the 2026.11.2 reinstall's key), not the strict image's throwaway `SHA256:I2VaMwa…`; `tenants.release` and `tenant_state.release` are empty; `tenant_state.state` is still `first_boot` while `tenants.state` is `provisioned`; the record was last updated 2026-11-10T04:46:50Z. **Missing proof:** the strict first boot records the measured throwaway key, the pin and the ready state. **Card: `t_22951d32` (ODL-398 follow-up).** |

---

## 4. The §5 re-cuts

### 4.1 The §1 rows

| v3.7 row | Decision | Verdict | Evidence / block |
|---|---|---|---|
| "One machine, one key, one owner." | cut | **PASS (cut)** | "One key" would read as a user SSH key; no user key exists (A1/A2). |
| "We never see the private half of your key." | cut | **PASS (cut)** | No user key; A2's throwaway has no private half. |
| "A root shell, for 24 hours — only when you grant it; never the passphrase." → "A root shell, for 24 hours — only when you grant it." | change | **PASS** | ODL-217 §8.1/§8.2 (A10 above): the 24-hour grant opens root only through the forced command; the expiry and the revoke each remove the key line and refuse a new login; every session logs `start`/`end`; the guest was restored to no grant. The row lands when ODL-217 merges. |
| "A machine is reserved for you. When you claim it, it is built with your key in its command line, and it waits for you." → "A machine is reserved for you. When you claim it, it is built and starts itself." | change | **PASS** | ODL-332: panel save → zero-touch boot → first visit 14:44:45Z → release 14:46:43Z → Webapp; no key, no wait. The claim half is ODL-459's page; the machine half is proven. |
| "The first connect compares the host-key fingerprint to the PASS line." | cut | **PASS (cut)** | The first connect is HTTPS in a browser; the certificate fingerprint is in the recovery file (`f903497d…` = served cert). Note: the image's SSH host keys are public and shared (§0.2) — irrelevant to the cut row, relevant to §8. |
| "The passphrase is typed over SSH and stays on the machine." | cut | **PASS (cut)** | No passphrase, no SSH. |
| "A lost Odile sign-in is reset on the machine with your boot key. There is no HQ reset." → "…with your recovery code. There is no HQ reset." | change | **PASS** | Reset drill 15:11:44Z (reset 200 + new code, new sign-in 200, old 401, used code refused); no HQ password store (`tenants` schema, 21:50Z). |
| "`odile-diag` bundles logs, versions, disk usage and service state. No passphrase, no private key, no prompt history, no file contents." → "…No password, no key, no prompt history, no file contents." | change | **PASS** (content); placement is ODL-474 | ODL-217 §8.4: the live bundle is 47,651 B (`bd1c7dd6…`); a planted bundle carrying a password, token, bearer and PEM was 43,197 B (`75afc3cb…`) and all 14 checks PASS from the fetched bytes; the live meter key, sign-in hash and workspace contents are absent (`diag-verdict.txt`, `planted-values.txt`). The drill hand-placed the program; the installer embedding that puts `odile-diag`/`odile-support` on a fresh tenant is **ODL-474** (`t_990d5405`) — the row's content is proven, its availability is that card's. |
| "It cannot read your passphrase, but it can add a LUKS keyslot or copy the volume key, so it can keep disk access after the grant." → "It can add a LUKS keyslot or copy the volume key, so it can keep disk access after the grant." | change | **PASS** | A root shell can: the first-boot seal itself adds a keyslot (`bb-sealed-provision`); the master key is copyable. No passphrase remains to name. |
| "Runs around the clock. V0.1 target: 99.0% uptime, with owner-waiting counted separately; V0.1.1 target: 99.5%. Both published on the status page." → "…99.0% uptime; V0.1.1 target: 99.5%. Both published…" | change (ODL-467) | **PASS at M1** | The targets are on `templates/status.html:92`; the page removes owner-wait from the ratio (`status/status.py`). The strict-bar uptime math and the two status rows are **ODL-467**, not this landing. |
| "Backed up nightly. Rebuildable in under an hour — with your key." → "Backed up nightly. Rebuildable in under an hour — without a key." | change (ODL-474) | **BLOCK** | No nightly backup and no restore drill yet (ODL-215 running). **Missing proof:** the nightly job and a restore into a rebuilt guest. **Card: ODL-215 (M2); lands with ODL-474.** |
| "The status page counts waiting-for-owner separately and names no tenant." | not re-cut | **PASS at M1** | The page still counts the state the heartbeat produces; the state itself is ODL-467's. |

### 4.2 The §5 FAQ answers

| Question (decided) | Verdict | Evidence / block |
|---|---|---|
| "Can Odile log in to my machine?" — "No. There is no user key and no private key for us to hold, no root password, no disk passphrase. We can stop or delete the machine, but we cannot read it." | **BLOCK** | Login half PASS (A1/A2/§4.2, pre-grant). "We cannot read it" is false for the disk: the master key is recoverable from the published image (§0.1); the pre-first-visit public sign-in is §0.2. **Missing proof:** first-boot re-key (disk) — **card `t_28dda3b7`**; the sign-in clause — **card `t_adcdec31`**. |
| "What is the boot key?" — "A public key in the measured command line whose private half was thrown away. It is plumbing, not a credential." | **PASS** | A2: public key in `/proc/cmdline`, private half shredded, no private half in git/logs/disk. |
| "What is the Odile sign-in?" — "The username and password that open your workspace. You set it on your machine at first visit, and it is hashed there; we never see it. It is the only app-layer gate: no 2FA in V0.1. A lost sign-in is reset with your recovery code." | **BLOCK** | A7's block: before the first visit the gate accepts the image's public bench password and the session secret is the image's (§0.2). The set-at-first-visit and reset halves PASS. **Card: `t_adcdec31`.** |
| "Do I need the boot key?" — "No. You never see it. The boot is sealed; your sign-in is the only thing you set." | **PASS** | A2/A7; no user action at boot; the first visit is the sign-in. (The seal/master-key caveat is A4, not this answer.) |
| "Can you reset my sign-in or unlock my disk?" — "No. There is no escrow and no passphrase. A lost sign-in is reset on the machine with your recovery code; if the disk will not open, the machine is rebuilt and you restore from your backup." | **BLOCK** | Reset half PASS (15:11:44Z drill). Restore half unproven (ODL-215); "no escrow" is true in the DB, but the disk key is in the published image (§0.1), so "unlock my disk" is not answered safely. **Missing proof:** ODL-215 restore + the re-key. **Cards: ODL-215; `t_28dda3b7`.** |
| "Can Servury read my disk?" — "After your first boot, no. Memory is encrypted by the CPU and the disk is LUKS2; they see ciphertext. They run the console, so never type a secret there." | **BLOCK** | Memory half PASS (policy 0x30000, DEBUG clear); console caution PASS (the console is Servury's serial). Disk half BLOCK: Servury holds the disk bytes; the master key is recoverable from the published image (§0.1). **Missing proof:** first-boot re-key. **Card: `t_28dda3b7`.** |
| "What happens after a reboot?" — "It starts itself. The disk key is sealed to the measured boot, so no one unlocks it; a repeating job that was due runs once when it comes back up." | **PASS (with A4's caveat)** | A5 (15:05 restart, 302 at 15:09; 20:43:45Z and 21:24 boots) + A11 (one catch-up at 15:07:54Z; a second at 20:44:43Z). The "no one unlocks it" half is A4's block (re-key); the row's reboot/catch-up facts are proven. |
| "Can you fix something for me?" — the full decided answer (odile-diag, grant, expiry/revoke, the handover scope, the keyslot/volume-key power, the log) | **BLOCK (the invocation path); the drilled clauses PASS** | The grant/expiry/revoke/log and the diag-content clauses are proven by ODL-217 §8 (§4.7, A10 above). But the answer tells the user to ask the agent to run `odile-diag` / `odile-support`, and the installer does not place either program on a fresh tenant yet — the drill uploaded them and removed them (`restore-final.txt`: `odile-support: command not found`). The handover scope and the keyslot/volume-key power are stated in the committed runbook §4/§6 but are capability statements, not drilled. **Missing proof:** a first boot that places `odile-support`/`odile-diag`, and a tenant where the agent runs them without a hand placement; the answer's post-revoke checks must also run as written on the tenant — `crontab` is not installed and the LUKS device is `/dev/sda3`, not the runbook's `vda2` (ODL-217 review M2). **Card: ODL-474 (`t_990d5405`)** — it also carries A10's sentence and the `/verify` grant row; the post-revoke command fix is **ODL-244 (`t_f3e1e9c2`)**. |
| "What happens if the machine dies?" — the full decided answer (rebuild, restore with the recovery file, the running-machine fallback) | **BLOCK** | The recovery file exists and its fingerprint matches (A9); the restore/rebuild is ODL-215. **Missing proof:** ODL-215's two drills. **Card: ODL-215; lands with ODL-474.** |
| "What do you store?" — "We store your email address, your payment record at Stripe, and the metadata needed to run your machine. At HQ we hold the boot key with no private half, a hash of your setup token, and the record of your machine. Not the disk key, not your sign-in." | **BLOCK** | The record/token/email half PASS (21:50Z tables). "Not the disk key" BLOCK (§0.1). **Missing proof:** first-boot re-key. **Card: `t_28dda3b7`.** |
| The unchanged answers: "Can I install anything?", "Where does the model run?", "What does inference cost?", "Where is the machine?", "Do you track me?", "Do you train on my data?", "Why only ten people?" | **PASS** | Accepted unchanged; the strict bar changes none of them. "Where is the machine?" says "backups are off-site" — the backup half is ODL-215 (flagged in §7); the Montreal location is Provider `location: Canada` (m1-tenant.md §2). "Do you train on my data?" stays conditional on NEAR AI's written confirmation. |

### 4.3 The privacy-inventory recovery-file row

| Decided row | Verdict | Evidence |
|---|---|---|
| "The recovery file — backup password and certificate fingerprint — to restore, and to pin the certificate — kept on your machine, root only; a copy is downloaded to you on your first visit." | **PASS** | First visit done page's download 200, 605 B, `text/plain`; `Certificate SHA-256: sha256:f903497d…` = the served certificate `F9:03:49:7D…:F4:DB` (14:57Z); the file is written 0600 root (`deploy/image/recovery-seed.py:96-103`; installer `DEFAULT_RECOVERY`), so "root only" holds; the agent's fallback is the live-machine route. The "to restore" purpose is ODL-215's, but the row's retention/delivery facts are proven. |

### 4.4 The §2 forbidden-table replacement

| Decided replacement | Verdict | Evidence / block |
|---|---|---|
| "There is no user key; the only sign-in is yours." (replaces "we hold your keys") | **BLOCK (the "only sign-in is yours" clause); the "no user key" half PASS** | "No user key" PASS: A2/§4.1 — the only authorized key is the measured throwaway public key `SHA256:I2VaMwa…`, its private half shredded, and HQ stores no private key. "The only sign-in is yours" is A1/§4.2's blocked clause: before the first visit the image's public bench password opens the workspace and the session secret is the image's (§0.2). **Missing proof:** a fresh boot where the bench password is refused before the first visit. **Card: `t_adcdec31`.** |

---

## 5. The three gaps

| Gap | Verdict | Evidence / block |
|---|---|---|
| 1. "The app layer is not hardware-measured yet." | **PASS** | The UKI measures firmware/kernel/initramfs/cmdline (the report's 48-byte launch digest 9f01725a…); Hermes/the installer/skills are inside the LUKS disk and pinned by the signed manifest (`/manifest` 200, `installer_sha256` 4aee80c5…; `release/manifest.json` 1ed9dcad…), not by the CPU; `/verify` carries gap 1 verbatim. |
| 2. "The console passes through the host." | **PASS** | The serial transcript is Servury's console (armed via `?action=serial`; used for the round-8 boot); the sign-in goes over HTTPS to the guest (first visit 14:57:46Z over the cert); the console caution is true. The live `/verify` page still carries the v3.7 "use SSH" wording — ODL-462 lands the decided gap 2; the decided sentence is proven. |
| 3. "Odile keeps three powers." — stop/delete, DNS, a different boot image ("detectable via the measurement … A changed image or firmware breaks the seal; the machine is rebuilt.") | **BLOCK** | Stop/delete PASS (the panel restarted the machine at 15:05:12Z; ODL-217's restarts at 20:43 and 21:24); DNS PASS (the `odl-fake01` record is DNS-only, 203.0.113.87); a different boot image PASS (eight panel pairs, eight measurements; the panel fields). The **power is understated**: a changed image carrying the recovered master key reads the disk without the seal (§0.1). **Missing proof:** first-boot re-key; **card: `t_28dda3b7`.** |

---

## 6. Adjacent deck rows the evidence also falsifies (outside the DoD list; flagged for the product lead)

These are in the current deck (v3.8a) and are not in the strict re-cut's §1/§5 lists. ODL-462 will leave them in place unless the product lead decides otherwise.

| Row | Verdict | Why |
|---|---|---|
| "We can stop it. We cannot read it." | **BLOCK** | The disk master key is recoverable from the published image (§0.1). The memory is encrypted; the disk is not sealed to the measurement. |
| "On a tenant machine, Odile cannot read the disk, the prompts or the files; a support grant can." | **BLOCK** | Same disk-key defect; the prompts/files are inside the disk. |
| "Memory is encrypted by the CPU. The hosting provider sees ciphertext." | **BLOCK (disk half)** | The memory half is proven (policy 0x30000, DEBUG clear); the disk half is §0.1. |
| "The encrypted backup is kept 30 days after cancellation, then deleted. Odile can delete it, not read it." | **BLOCK** | No backup exists yet (ODL-215); the recovery-file backup password is guest-generated, but the backup itself is unproven. |
| "The machine is in Montreal, Canada; backups are off-site." | **BLOCK (backup half)** | Montreal PASS (Provider `location: Canada`); the off-site backup is ODL-215. |
| "The machine is an AMD SEV-SNP confidential VM. The report is served live at `/attestation`." | **PASS** | `/attestation` 200, measurement 9f01725a…, report version 5; the page calls it a boot report, not a liveness proof. |
| "The boot is measured: firmware, kernel, initramfs and command line are hashed into a value the CPU signs." | **PASS** | The tenant report signature verified against the AMD KDS VCEK chain; the measurement recomputed from the published UKI. |
| "Each row is tagged: checked from your laptop, or read from the machine. A root shell can change the machine rows." | **PASS** | `/verify` shows the `read from your machine` tag and the access-report timestamp; the root caveat is true. |
| "The disk is encrypted with LUKS2, with a passphrase you set at first boot." (v3.7) | **must not land** | The strict bar has no passphrase; it is replaced by the §3 disk row, which itself is BLOCKED on the master key. ODL-462 must not leave the v3.7 sentence. |

The v3.7 access rows that the re-cut replaces (A1–A11 old, the boot-key/passphrase/waits rows) are **not** decided rows; the verdict is on the decided text. The current public pages still carry v3.7 until ODL-462 lands; that is the landing card's job, not a proof of the decided rows.

---

## 7. Block summary — missing proof and the card

| Blocked rows | Missing proof | Card |
|---|---|---|
| A3, A4, A8 ("Not the disk key"), §3 disk row, §4.3, §4.8, gap 3, "Can Odile log in…" ("cannot read it"), "Can Servury read my disk?", "What do you store?", "We can stop it. We cannot read it.", "On a tenant machine, Odile cannot read…", "Memory is encrypted…" (disk half) | A first boot that **re-keys the LUKS volume** with a guest-generated master key (then seals it), so the published image + bootstrap key no longer opens the tenant disk; the header test must show the master key from the published image no longer opens the live volume. | **`t_28dda3b7` — ODL-454 follow-up** (`deploy/image/bb-sealed-provision`, `deploy/image/sealed-image.sh`, `tests/test_sealed_image.py`, `docs/runbooks/image.md`). ODL-454 is done; no existing card owned the re-key. |
| A1 ("the only sign-in is yours"), A7, §4.2 ("only sign-in is yours"), the §2 forbidden-table replacement ("There is no user key; the only sign-in is yours."), the §5 "What is the Odile sign-in?" answer | A fresh boot where the gate is **shut until the first visit**: the image's public bench password refused, the signing secret generated per tenant, no SSH host private key in the image. | **`t_adcdec31` — ODL-456 follow-up** (`deploy/image/install-stack.sh`, `odile-firstboot.py`, `mkdisk.sh`, `installer/odile_install.py`, `installer/first_visit.py`). |
| §4.11 ("HQ stores the throwaway public key") | The strict first boot records the measured throwaway public key (`SHA256:I2VaMwa…`), the pinned release and the ready state in `tenants`/`tenant_state`, not the 2026-11-2 `IMdWy…` row. | **`t_22951d32` — ODL-398 follow-up** (the provisioning writer of the tenant row; ODL-398 and its two follow-ups are done and did not cover the strict boot). |
| A9 (backup clause), "Backed up nightly…", "The encrypted backup is kept 30 days…", "The machine is in Montreal…" (backup half), "Can you reset my sign-in or unlock my disk?" (restore half), "What happens if the machine dies?" | The nightly backup and the two restore drills (delete/restore; destroy/rebuild/restore with the recovery file). | **ODL-215 (M2, running)**; the sentences land with **ODL-474**. |
| "Can you fix something for me?" (the invocation path) | The installer places `odile-support`/`odile-diag` on a fresh tenant, so "ask the agent to run it" is true without a hand upload; A10's page row and the `/verify` grant row also ride this card. | **ODL-474 (`t_990d5405`)**. The mechanism rows (A10, §4.7, "A root shell, for 24 hours", "`odile-diag`…") are drilled and committed at `odl/ODL-217` `e69dd7f` §8; they land when ODL-217 merges unchanged. |
| The audience row's "Claiming takes 3 minutes" | A clocked claim-page submission (open → email+consent → confirm) on the live page, or the number drops. | **ODL-462** (the landing card; the words are the product lead's). |
| §4.4 (optional laptop verifier) — **closed 13 November 2026** | The addendum records the PASS: the tenant's own VCEK (AMD KDS), the pinned release 2026.11.2 and `--expect` `9f01725a…`; a changed byte fails; the demo still passes. | **`t_8a0ef011`** (the fix); `t_91b95124` lands the command. |
| §4.5 (command block) | The UKI-form `sev-snp-measure` command that reproduces `9f01725a…` from the published UKI (`--kernel odile-tenant.efi`), not the loose `vmlinuz`/`initrd.img` form that reproduces `cdcfc9f8…`. | **ODL-462** (the command block; the claim sentence is unchanged; the builder's `measurements.txt` has the exact command). |
| The status/reliability owner-wait row | The status page's uptime math without the owner-wait bucket; the two status rows. | **ODL-467** (already the planned owner). |

**A note the product lead must not miss:** the Architect's card `t_bf99b807` says the seal "generates a fresh volume key on the guest". It does not — it generates a fresh **keyslot passphrase**. `bb-sealed-provision`'s own header says a first-boot custom image "would luksFormat the root with V as its only keyslot instead" (`deploy/image/bb-sealed-provision:15,110-115`). `t_bf99b807` should say "fresh keyslot key until the re-key lands", or the record will repeat the §0.1 overclaim in the V0.1 history.

---

## 8. What remains trusted, not verified

### 8.1 Secret handling (the seat's own check)

- **The throwaway boot key:** public half only in the measured line, the pin and the image; the private half is shredded at build and absent from git, the build logs and disk (A2). PASS.
- **The setup token:** a **single-use, instance-bound, 24-hour** token in the measured line. It is public by design (the host reads the panel) and spent at the first boot. The current tree's committed pin carries the spent round-8 token (`67de1ba9…`, live to 2026-11-13T10:23Z); `./tools/secret_scan.py --tree` passes (23:13Z, 667 files, 21 allowlisted). The **history** scan (`git rev-list --all`, 932 commits, 23:13Z) reports **8** findings, none in this branch's tree: the 2 spent round-7 tokens in `docs/ops/evidence/uki-cycle/…` at commit `1c17d27`, and 6 on the **sibling branch** `odl/uki-cycle` at `007eeef` — the same class, a 64-hex `odile.token=` setup token in a `t_c1e0f336` cycle-boot log (`boot-*.txt` lines 8/128, `cycle-*.md` line 29). That sibling merged into `main` at `898d493` while this verdict was being re-handed; the **tree** scan on `main` `898d493` still passes (687 files, 12,894,582 B, 29 allowlisted) because those evidence files are allowlisted, so the 6 findings are history-only. All are single-use, instance-bound and spent; a redaction/allowlist cleanup remains a follow-up for **ODL-334**, not an access-claim block.
- **The tenant sign-in and recovery code:** not in git or this file; the sign-in is a scrypt hash on the guest (`/verify` line); the recovery code is shown once and rotated (the drill used a code issued by the previous reset).
- **The recovery-file backup password:** in `/etc/odile/recovery.txt` on the guest (0600 root) and in the first-visit download (the user's); the builder's `/var/tmp/odile-332-run/recovery-final.txt` is a test artifact, not in git. The 0600 mode is in `installer/odile_install.py` (`write_recovery` → `write_atomic(..., 0o600)`) and `recovery-seed.py`.
- **The LUKS master key:** dumped only to a 0600 temp file during this audit, hashed (`74f2552b…`), and deleted; the audit log records the hash, never the key. The finding is §0.1.
- **ODL-217's evidence attachments (new):** the drill transcript `verify-and-diag.txt` (attachment 302) carries the tenant's live 32-char meter key and 86-char scrypt sign-in hash; the ODL-217 round-1 review (M6) asks for a redacted replacement. It is a card attachment, not git, and A10's evidence does not depend on it (`grant-use-expire.txt`, `revoke.txt`, `reboot*.txt`, `diag-verdict.txt` are the proof). The drill's program hash (`eb06772a…`) predates the whitespace-only `--reason` patch; the reviewed head is `3f710593…` and the reviewer's live smoke of it passes (M4). The runbook's §8 exact-sentence defects (M5) are ODL-217's to fix; the mechanism is unaffected.
- **The published image's secrets (new, §0.2):** the image carries `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH` (scrypt of the public bench password), `HERMES_DASHBOARD_BASIC_AUTH_SECRET` (the live session-signing key, shared by every tenant) and `/etc/ssh/ssh_host_{ed25519,rsa,ecdsa}_key` (private host keys). None is a *user* credential; the first is a public default sign-in until the first visit, the second is proven live by the forged-token test, the third lets anyone impersonate the SSH host. Card `t_adcdec31`.

### 8.2 The trust anchors

- **AMD's root.** The VCEK→ASK→ARK chain verifies to the ARK whose SHA-256 `69:d0:63…` is pinned in `tools/snp_report.py`; the ARK's publication is AMD's. The tenant's VCEK was fetched from AMD's KDS for chip `3fa18b…` and TCB `bl 4 / tee 0 / snp 23 / ucode 213`; the report's **committed** TCB is snp 28 (≥ 27, AMD-SB-3020), while the hypervisor reports snp 23 (`SNP_SET_REPORTED_TCB`), so the committed value is the one checked.
- **The image's signature.** The UKI is signed by Odile's image key (`sbverify OK`; cert SHA-256 `5A:21:B4…`), an **operator** signature, not a hardware measurement. The measurement is over the UKI and the OVMF; the OVMF is Servury's published 2026.11.1 (`8731846c…`). The build is not byte-reproducible (`sbsign` changes the signed UKI); the measurement is a pair with the published file (5cb31808… → 9f01725a…).
- **The app layer.** Not hardware-measured. The release manifest is signed by Odile's Ed25519 operator key; the installer/skills are pinned by it. A signed manifest proves provenance, not innocence.
- **The report's freshness.** `/attestation` is the boot report (`fetched_at` 21:24:00Z), not a liveness proof; `report_data` carries a fixed value, so a genuine report from the same machine can be replayed. The page says "boot report, not a liveness proof".
- **The host's other powers.** Servury runs the console, can stop/delete the machine, and the disk bytes live on its host. Odile controls DNS and the panel's boot/disk image fields (eight rounds, eight measurements). Gap 3 names these; the master-key power is §0.1.
- **The first boot.** The published disk image opens with the bootstrap key at the first boot (the image is built on a host with no AMD PSP); the boot re-seals a keyslot and shreds the key. Until the re-key, the master key survives; the image's published bootstrap key opens the image read-only (21:18:59Z).
- **M2 drills.** The support grant is drilled and committed at `odl/ODL-217` `e69dd7f` §8; it lands when that card merges. The backup/restore (ODL-215) is still running; its sentences are held for ODL-474.
- **The tenant record.** `tenants.ssh_public_key` still carries the pre-strict owner public key (`IMdWy…`); `tenants.release` and `tenant_state.release` are empty; `tenant_state.state` is `first_boot` while `tenants.state` is `provisioned`; `tenant_state.detail.boot_key` is the same old key and `initramfs_host_key` is the old host key `fFic3fm3…`; the record was last updated 2026-11-10T04:46:50Z. Card `t_22951d32`.
- **The live machine's state.** ODL-217's drill rebooted the machine twice on the final run (21:15 and 21:24); the 21:18Z read caught a live grant (`operator key FAIL`) — that is the drill, not the handoff state. The post-drill state was restored at 21:39:57Z / 22:00:08Z and re-read at 23:12Z: `/attestation` `fetched_at` 21:24:00Z, `/verify` five PASS on the 21:27:15Z access report.

---

## 9. Reproduction

The evidence files are under `/var/tmp/odile-466-run/` (audit logs; the master key itself was deleted, only its SHA-256 is recorded) and `/var/tmp/odile-332-run/` (the builder's run; serial, drill and page captures). The main commands:

```sh
# live report, page, manifest, gate (re-read 23:12Z: report fetched_at 21:24:00Z, /verify five PASS on the 21:27:15Z access report)
curl -sS https://odl-fake01.odile.fyi/attestation | jq '{measurement,fetched_at,release,report_version,report_size}''
curl -sS https://odl-fake01.odile.fyi/verify | grep -E 'usable keys|boot key|seal|sign-in hash|operator key'
curl -sS https://odl-fake01.odile.fyi/manifest | sha256sum
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' https://odl-fake01.odile.fyi/
ssh -F /dev/null -o BatchMode=yes -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no root@203.0.113.87 true; echo exit=$?
# the tenant's own state through the Webapp session (the keyless read path)
/usr/bin/python3 /var/tmp/odile-466-run/live_read.py
/usr/bin/python3 /var/tmp/odile-466-run/live_read2.py
/usr/bin/python3 /var/tmp/odile-466-run/live_read4.py     # 21:04:29Z, seal/access report/heartbeat
# the report against AMD's KDS VCEK (not the vendored demo VCEK)
/usr/bin/python3 /var/tmp/odile-466-run/verify_tenant_report.py
# the published release
curl -sS https://odile.fyi/releases/m1/odile-tenant.efi -o /var/tmp/odile-466-run/published-tenant.efi
sev-snp-measure --mode snp --ovmf /var/tmp/odile-466-run/OVMF.amdsev.fd --kernel /var/tmp/odile-466-run/published-tenant.efi --vcpus 4 --vcpu-type EPYC-v4 --guest-features 0x1
sbverify --cert deploy/image/odile-image-signing.crt /var/tmp/odile-466-run/published-tenant.efi
# the optional laptop verifier, as published (it fails on a tenant: §4.4)
curl -fsSLO https://odile.fyi/odile-verify.tar.gz && sha256sum odile-verify.tar.gz
# ODL-217's drill (the A10 evidence): docs/runbooks/support.md §8 at odl/ODL-217 e69dd7f;
# raw transcripts at /root/.hermes/kanban/boards/odile/attachments/t_5bd24b98/

# the disk key (§0.1)
/var/tmp/odile-466-run/masterkey_test.sh          # 21:17:19Z: MK unchanged, bootstrap refused, MK opens
/var/tmp/odile-466-run/image_open_test.sh         # 21:18:59Z: the image opens with its own bootstrap key
# the sign-in gate (§0.2)
/usr/bin/python3 /var/tmp/odile-466-run/bench_hash_check.py  # image hash = public bench password, 21:57Z
/usr/bin/python3 /var/tmp/odile-466-run/forge_test.py     # image secret + binding -> 200; alone -> 401
# the HQ record (read-only; no email printed)
sqlite3 "file:/var/lib/odile/waitlist.db?mode=ro" \
  "select slug,state,ssh_public_key,release from tenants; select * from tenant_state;"
# the gates (23:29Z, gate head acc3e1e / main 898d493)
./tools/claims_lint.py        # claims lint: OK (20 files, 14 forbidden phrases)
./tools/secret_scan.py        # PASS: no secret in 667 files, 12,329,548 bytes
./tools/secret_scan.py --history   # 8 findings on other refs (2 at 1c17d27, 6 on odl/uki-cycle 007eeef)
make acceptance CARD=ODL-466  # worktree clean; merges into main; evidence present; card-acceptance: READY
```

The two test scripts are read-only on the published image; the master-key test mutates only a 64 MiB sparse device restored from a LUKS header backup, and the master key is written 0600, hashed and shredded. The image-open test mounts the image and the image's root read-only.

---

## 10. The handoff

**Verdict file:** this file, committed on `odl/ODL-466` (branch base `8c2ad99`, ODL-332 merged); the gate is re-run at `main` `898d493`.
**Per-row evidence:** the tables above; each row carries its evidence link and UTC timestamp.
**Repo gate:** `make acceptance CARD=ODL-466` at `main` `898d493` — worktree clean, `odl/ODL-466` merges into `main` with no conflict, evidence present, `card-acceptance: READY`, `make acceptance: OK ODL-466` (full output pasted in the handoff).
**The two sentences to read:** (1) the seal wraps a keyslot, the master key is still the build-time key, and the published image + bootstrap key recovers it — so the disk key is not sealed and Odile holds it until the first boot re-keys the volume (§0.1); (2) the published image carries the public bench sign-in hash and the live session-signing secret, so for the 13 minutes before the owner's first visit anyone with the URL could be inside the workspace (§0.2).
**Cards opened:** `t_28dda3b7` (re-key), `t_adcdec31` (image secrets / first-visit gate), `t_22951d32` (tenant record), `t_8a0ef011` (odile-verify on a tenant). Existing owners: ODL-215 (backup/restore), ODL-217 (grant drill — now committed at `e69dd7f`), ODL-474 (`t_990d5405`: the support answer's invocation path and A10's page row), ODL-462 (command block and the untimed "3 minutes"), ODL-467 (status).
**What the product lead must decide (not a rewording by security):** whether v3.9 lands with the master-key rows and the pre-first-visit sign-in rows held/cut (the deck can carry a decided row ahead of its proof; the public pages may not), or waits for `t_28dda3b7` and `t_adcdec31`. A10's drill is committed at `odl/ODL-217` `e69dd7f` and lands when that card merges; the answer's invocation path and A10's page row ride ODL-474. The backup sentences remain held for ODL-215. The untimed "3 minutes" is ODL-462's to carry or drop. ODL-462 cannot land a blocked row.
