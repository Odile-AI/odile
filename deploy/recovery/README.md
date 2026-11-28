# Odile factory recovery — read this first

**If HQ is gone, this folder brings it back on a new server.** You need about
45 minutes. You do not need to know Linux; copy the four commands exactly.

Keep this folder in **1Password** and on a **USB stick**. It is the only copy
of the backup password and the Cloudflare keys. If you lose it, the backup
cannot be opened. Do not email it.

---

## 1. Make a new server

In any server provider's web console (Hetzner, DigitalOcean, Vultr, Linode, …):

- Ubuntu 26.04 LTS
- 2 vCPU, 4 GB RAM, **40 GB disk**
- Add your laptop's **SSH key**
- Note the server's **IPv4 address** (four numbers, like 203.0.113.10)

You do **not** need Servury.

## 2. Copy this folder to the new server

On your laptop, open a terminal in the folder **above** this one and run:

```sh
scp -r odile-factory-recovery root@NEW_IP:/root/
```

Replace `NEW_IP` with the address from step 1.

## 3. Run the recovery

```sh
ssh root@NEW_IP
cd ~/odile-factory-recovery
bash recover.sh
```

It prints what it is doing. Do not close the window. It takes about 45 minutes.
It also prints which Hermes version it installs; if the exact build the backup
recorded was never published, it installs the nearest published release and
says so. That is expected — leave it alone.

It starts everything those three names need: the site, the desktop backend
behind `gw.odile.fyi`, and the test bench at `test.odile.fyi`. The **chat bot**
(Matrix/Telegram) is left stopped on purpose — a restored `.env` may have a
stale device — and starts later with:

```sh
systemctl --user enable --now hermes-gateway.service
```

If you want to see the plan before it changes anything, run
`bash recover.sh --dry-run` first (it needs `restic`:
`apt-get install -y restic`). It prints the Hermes version, the old server's
address, this server's IPv6 (or `none`, which means the old AAAA records are
deleted) and the DNS names, and changes nothing.

## 4. Wait for `RECOVERY COMPLETE`

When it finishes, wait five minutes, then open:

```
https://odile.fyi
```

## 5. Only if you need to install a new machine — the release images

The machine images the server panel downloads are **not** in the backup (they
are 32 GB of build output). After a restore, `https://odile.fyi/releases/...`
answers **404**, so the panel cannot install a new machine until one build is
published. The site itself is fine; only those download links are missing.

The script prints these four commands during the recovery, when it finds the
release directory empty. A person who knows Linux runs them once, on the new
server, and pastes the two lines each one prints into the server panel:

```sh
cd ~/odile
sudo ./deploy/image/sealed-image.sh --out deploy/image/out-sealed
./deploy/image/uki.sh --in deploy/image/out-sealed --sign --pin deploy/image/tenant-image.pin
./deploy/caddy/deploy_releases.py --publish-disk deploy/image/out-sealed/odile-tenant-sealed.raw
./tools/publish_uki.py --uki deploy/image/out-sealed/odile-tenant.efi
```

Paste each `URL` + `SHA-256` pair into **Manage → Disk image** and **Manage →
Boot image**. The rule for keeping old builds, and what to prune, is
`docs/runbooks/release.md` §7.

## 6. Only if it says `DNS NOT UPDATED`

Open the Cloudflare dashboard for `odile.fyi` → DNS. Change the **A** records
for `odile.fyi`, `gw.odile.fyi`, `mail.odile.fyi`, `testing.odile.fyi` and
`test.odile.fyi` to the new IPv4 the script printed.

Each of those names that also has an **AAAA** record must change too. If the
script printed a `New IPv6`, point the AAAA at it. If it printed `New IPv6:
none`, **delete** the AAAA record — otherwise the name still answers with the
old server's IPv6, and mail sent to `mail.odile.fyi` goes to the dead box. In
the SPF text at `mail.odile.fyi` (`v=spf1 …`), change `ip4:<old-ipv4>` to
`ip4:<new-ipv4>` and change `ip6:<old-ipv6>` to the new IPv6 — or delete that
`ip6:` token when the new server has no IPv6.

Leave **alone**: `demo.odile.fyi` (the demo tenant), `www`, and every hostname
that starts with `odl-` (the tenant guests), AAAA records included. Wait five
minutes.

---

## If something fails

Open `/var/tmp/odile-recovery.log` on the new server and send it to
**help@odile.fyi**. The last line that starts with `==` names the step that
stopped.

The technical appendix is `docs/runbooks/backup-hq.md` §5A.

## What is in this folder

| File | What it is |
|---|---|
| `README.md` | this page |
| `recovery.env` | the secrets: backup password, R2 token, Cloudflare token, zone |
| `recover.sh` | the one command that performs the restore |
| `manifest.json` | the Hermes build, old IPv4 and DNS names known when the kit was made |
| `CHECKSUMS.txt` | SHA-256 of the files, so you can check the kit was not changed |

The **facts** the restore needs — the Hermes commit, the old IPv4 and the DNS
names — are written by the nightly backup into the snapshot's
`manifest.json`. This folder is only the secrets and the script; it does not
have to be refreshed when HQ changes. The kit's own `manifest.json` is the
fallback for a snapshot taken before the backup wrote those facts.

**Never edit `recovery.env` unless a token was rotated. Never send it to
anyone.**
