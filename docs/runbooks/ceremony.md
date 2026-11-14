# First boot — verify, encrypt, unlock, install, sign in

> **Superseded on 2026-11-10 by the strict bar — this page is the pre-strict
> Model A ceremony, kept as the record.** It is no longer the path a tenant
> runs: the Product Lead ruling of 2026-11-10 deleted the ceremony, and the
> first tenant boots a custom measured UKI whose disk is the pre-sealed image
> the provider panel writes. No passphrase is set, no boot key is held, no
> terminal is opened. The live run is `docs/runbooks/install.md` §12; the
> measured state is `docs/evidence/m1-tenant.md`.
>
> Two corrections are applied below because this page is still cited: the
> machine is on Servury release **2026.11.2** (not 2026.11.1) with initramfs
> host key **`SHA256:fFic3fm3N0kx5c8MC4SaJugl7pRusjaR70Gdr/G2WgM`**, and the
> machine-kit JSON block §4 used to quote no longer exists (ODL-456).

One page for the person the machine belongs to. The machine waits at Servury's
measured initramfs. You check the boot, set the disk passphrase, unlock, run one
signed installer, and sign in.

Four movements, then the product moment:

> **The boot key opens the machine at boot. Your Odile sign-in opens the
> workspace, in the browser.** — `docs/CLAIMS.md` §1 Access
>
> **The boot key is not the login.**

Claiming takes 3 minutes; setup is about 20 minutes — a verifier, a disk
passphrase, one signed installer (`docs/CLAIMS.md` §1 First run).

---

## 0. Before you start

Set these three values once, in the shell you will keep open. The ready email
carries the first two; the third is where your private key lives.

```sh
IP=203.0.113.87          # the address in your ready email
REL=2026.11.2            # the release id in your ready email (2026.11.1 before 2026-11-10)
KEY=~/.ssh/odile         # the private half of your boot key
```

You also need:

- **The `odile` checkout** — it holds `odile-verify`, `odile-verify.pin`, the
  wrapper below, and the signed installer in `installer/`. Run every command
  from its root.
- **The verifier's Python.** Servury's `blackbox-verify` needs the
  `sev-snp-measure` and `cryptography` modules, and Debian 13 refuses
  `pip install --user` (PEP 668). Make a venv in the checkout root, activate
  it, and stay in that shell: the wrapper uses the active venv.

  ```sh
  python3 -m venv .venv        # Debian: sudo apt install python3-venv first
  . .venv/bin/activate
  pip install sev-snp-measure cryptography
  ```

  If you open a new shell, run `. .venv/bin/activate` again, or add
  `--python .venv/bin/python` to the wrapper.
- **A second copy of your private key**, somewhere other than this laptop. A
  lost boot key cannot be rescued after a reboot (§6).
- **Six lowercase words, separated by spaces** — the disk passphrase you will
  set. Write them only where you alone can read them; nobody can recover them.
- **The release directory** `2026.11.2/` (`OVMF.amdsev.fd`, `vmlinuz`,
  `initrd.img`, `SHA256SUMS`, `RELEASE.json`). The pin names the release URL;
  §1 downloads it.

A plain `ed25519` key is required. Dropbear in the initramfs may not accept a
FIDO/`sk-` key. **If the private half lives in 1Password or Bitwarden** and your
SSH agent serves it: use `--public-key <your>.pub` (for example
`~/.ssh/odile.pub`) instead of `--boot-key "$KEY"` in every wrapper command,
and drop `-i "$KEY" -o IdentitiesOnly=yes` from every `ssh` and `scp` below; the
agent supplies the key.

## 1. Verify — before any passphrase

The initramfs serves no HTTPS. The wrapper fetches the attestation over SSH,
runs Servury's verifier on the saved report, then runs our verifier on the same
report, and only writes `known_hosts` if everything passes.

```sh
curl -fsSLO https://servury.com/downloads/blackbox/blackbox-verify
sha256sum blackbox-verify
# 8c937f02a94e83e7618a98b16a961a0f09b309cefa4100abc1f05e994df30f97

mkdir -p "$REL" && cd "$REL"
curl -fsSLO "https://servury.com/downloads/blackbox/$REL/OVMF.amdsev.fd"
curl -fsSLO "https://servury.com/downloads/blackbox/$REL/vmlinuz"
curl -fsSLO "https://servury.com/downloads/blackbox/$REL/initrd.img"
curl -fsSLO "https://servury.com/downloads/blackbox/$REL/SHA256SUMS"
curl -fsSLO "https://servury.com/downloads/blackbox/$REL/RELEASE.json"
sha256sum -c SHA256SUMS
cd ..
```

Servury's `SHA256SUMS` is unsigned; the anchor is the three artifact hashes in
`odile-verify.pin`, and the wrapper checks the release directory against them
before it trusts the verifier.

```sh
./scripts/verify_and_unlock.sh --host "$IP" --release "./$REL" \
    --boot-key "$KEY" --record boot-record.json
```

On PASS you will see, among the rows:

```
  boot key       PASS  ssh-ed25519 SHA256:<your key> (the key you hold)
  bb.ip4         PASS  <ipv4> (expected <ipv4>)
  wire key       PASS  SHA256:<the machine's host key> is the key on the wire
  measurement    PASS  <96 hex characters>
  PASS — verified.
```

Write down the `measurement` line: the installer asks for it in step 4.
`--record boot-record.json` saves it; a later boot can pass
`--previous boot-record.json` and the verifier prints what moved. The wrapper
has also written the machine's host key to `~/.ssh/known_hosts`.

> ### Stop sign 1 — the first connection
>
> If you connect **without** the wrapper, SSH asks:
>
> ```
> The authenticity of host '<ipv4>' can't be established.
> ED25519 key fingerprint is SHA256:...
> Are you sure you want to continue connecting (yes/no/[fingerprint])?
> ```
>
> Compare that fingerprint with the `wire key` row from PASS. Type `yes` only
> if they match. The first connect compares the host-key fingerprint to the PASS
> line (`docs/CLAIMS.md` §1 First run).

> ### Stop sign 2 — a changed host key
>
> ```
> @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
> @    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
> @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
> ```
>
> **Stop. Do not type the passphrase.** Re-run the wrapper. A release move
> changes the host key once, and a PASS accepts the new one; a FAIL means this
> is not the machine you ordered. The wrapper prints `known_hosts CHANGED`
> instead of overwriting a key silently.

## 2. Encrypt — set the disk passphrase

```sh
./scripts/verify_and_unlock.sh --host "$IP" --release "./$REL" \
    --boot-key "$KEY" --mode encrypt
```

The wrapper verifies again, then opens the initramfs's `encrypt` command over
`ssh -t`. You will see the vendor's own prompt:

```
  Blackbox - Disk Encryption Setup
...
Only continue if blackbox-verify accepted this VM's attestation.
Press Enter to continue, or Ctrl+C to cancel...
Passphrase:
Verify passphrase:
```

Type the six words twice. The initramfs salts them with argon2id; its keymap is
US. The disk is encrypted in place and the same session carries on into the
boot — if it closes instead, reconnect and run `--mode unlock` (§3).

**The passphrase goes only to the initramfs over SSH.** Never type it in the
Servury VNC or serial console, the Webapp terminal, or chat, and never send it
to Odile. The console passes through the host; serial and VNC can be watched
(`docs/CLAIMS.md` §3, "The console passes through the host").

## 3. Unlock — every later boot

After a reboot the machine waits for you. This is the lock, not downtime
(`docs/CLAIMS.md` §1).

```sh
./scripts/verify_and_unlock.sh --host "$IP" --release "./$REL" \
    --boot-key "$KEY" --mode unlock --previous boot-record.json
```

Press Enter, then type the passphrase once:

```
Please unlock disk root_crypt:
  Disk unlocked.
```

That is the whole unlock in one line — the wrapper's last step, over the host
key it just verified:

```sh
ssh -t -i "$KEY" -o IdentitiesOnly=yes "root@$IP" unlock
```

With a vault key, drop `-i "$KEY" -o IdentitiesOnly=yes` from that line; the
agent supplies the key.

The session carries on into the boot. The measurement and the host key are the
same as the first boot; a release move changes both once, `--previous` prints
what moved, and the wrapper accepts the new host key only on PASS.

## 4. Install — one signed command

Copy the three release files from the checkout's `installer/` folder to the
machine — the same three your recovery kit names — then run the installer as
root. The installer checks its own signature before the first change, checks the
boot report against the number the wrapper printed, and asks for your **Odile
sign-in**.

```sh
scp -i "$KEY" -o IdentitiesOnly=yes \
    installer/odile_install.py installer/odile_install.py.sig \
    installer/odile-signing-key.pub "root@$IP:/root/"
ssh -i "$KEY" -o IdentitiesOnly=yes "root@$IP"
```

On the machine, check the signature and run the installer. The machine-kit JSON
block this page used to quote is **retired**: `templates/recovery-kit.md` no
longer carries it (ODL-456), and the strict bar never runs this section by
hand. The installer still accepts `--recovery-kit FILE` when one is in hand
(the file carries no private key and no passphrase); a strict-bar image sources
`odile.token` and `bb.iid` from its measured command line instead.

```sh
cd /root
sha256sum odile-signing-key.pub
python3 odile_install.py --verify-signature --pubkey odile-signing-key.pub
curl -s https://odile.fyi/odile-signing-key.pub | sha256sum
python3 odile_install.py --recovery-kit <kit.json> --expect <measurement>
```

The two `sha256sum` values must match, and the signature line must say `OK`. The
installer reads the signature from `odile_install.py.sig` beside it — that is why
the `.sig` is copied. A kit carries no private key and no passphrase; under the
strict bar the first boot supplies the tenant facts itself.

`<measurement>` is the 96-hex `measurement` line from §1, copied exactly. The
sign-in is 16 characters or more, or blank to generate 32 and print them once.
It is hashed on the machine with scrypt; only the hash reaches `.env`
(`docs/CLAIMS.md` §1 Access). Odile never sees it.

The installer's step-by-step contract is `docs/runbooks/install.md`.

## 5. The first product moment

Open `https://<slug>.odile.fyi`, sign in with the Odile sign-in you just set,
and the workspace opens. That is the product gate.

**The boot key got you into the machine. It is not the login.** The boot key is
plumbing for the boot chain; the Odile sign-in is the front door
(`docs/CLAIMS.md` §5, "Why do I need both?").

That is the ceremony: verify → encrypt → unlock → install → sign in.

## 6. If the boot key is lost

A lost boot key is recoverable **only while the machine is running**. After a
reboot, the initramfs accepts exactly one key — the one in the measured command
line — and a support grant cannot reach it.

- Keep a second copy of the private key off the laptop, in a vault you control.
- While the machine is running, add a second key to the booted system:

  ```sh
  echo 'ssh-ed25519 AAAA... you@laptop' >> /root/.ssh/authorized_keys
  ```

  It does not open the initramfs on the next boot; only `bb.key` does. It keeps
  a way in while the machine is up, which is the only window in which a lost
  boot key can be worked around.
- After a reboot without the boot key, the recovery is the backup password you
  saved off the machine, plus a rebuild (`docs/CLAIMS.md` §5, "Can you reset my
  password or unlock my disk?").
- A lost **Odile sign-in** is reset on the machine with the boot key. There is
  no HQ reset. (Strict bar: with no usable boot key, the reset is the recovery
  code printed once at the first visit — the done page's recovery file, or the
  reset form. A broken seal is rebuild plus restore.)

## 7. The pin — for whoever ships the next machine

The release must be in `odile-verify.pin`; a machine on an unpinned release
fails, never falls back to the newest entry. Before a new release carries a
tenant, re-run the external audit on the release bundle and add its entry:

```sh
/root/bin/verify-blackbox.sh --deep 2026.11.2     # HQ, before a tenant
```

## What was measured, and on what

The first tenant is `odl-7971a8`, `203.0.113.87`, on Servury release
`2026.11.2` as of the 2026-11-10 reinstall. The pre-strict machine carries the
owner's public key `SHA256:zaZC3CG0yGBGyTbJ8NR8JnL8pzL2Xao1vR2cgYQFkC0` and the
initramfs host key `SHA256:fFic3fm3N0kx5c8MC4SaJugl7pRusjaR70Gdr/G2WgM`; the
strict image replaces the first with a throwaway key whose private half is
shredded, and keeps one host key per machine.

On 2026-11-09 the verify, encrypt and unlock steps were rehearsed against a
local initramfs replica (Servury's exact prompts) with the demo host's **real**
SEV-SNP report and the published release:

| Step | Measured | Where |
|---|---|---|
| verify (wrapper, both verifiers, AMD chain) | 1.4 s | real report, real release |
| encrypt (prompts and hand-off) | 1.8 s | replica; the disk encryption itself is minutes |
| unlock (Enter, passphrase, boot) | 1.8 s | replica; a real reboot adds the boot |
| install | — | `docs/runbooks/install.md`; timed on the tenant in ODL-332 |
| first sign-in | — | timed on the tenant in ODL-332 |

The wrapper also ran the verify step twice through the §0 venv — once with the
venv active and once with `--python .venv/bin/python` — and both passed.

The rehearsal's scripts and transcripts are in
`/root/.odile/review-evidence/odl-202/`. The real machine's own encrypt, reboot
and unlock times are ODL-332's measurement, because the private half of its boot
key is held by its owner, not at HQ.
