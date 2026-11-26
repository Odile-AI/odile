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

If you want to see the plan before it changes anything, run
`bash recover.sh --dry-run` first (it needs `restic`:
`apt-get install -y restic`). It prints the Hermes version, the old server's
address and the DNS names, and changes nothing.

## 4. Wait for `RECOVERY COMPLETE`

When it finishes, wait five minutes, then open:

```
https://odile.fyi
```

## 5. Only if it says `DNS NOT UPDATED`

Open the Cloudflare dashboard for `odile.fyi` → DNS. Change the **A** records
for `odile.fyi`, `gw.odile.fyi`, `mail.odile.fyi`, `testing.odile.fyi` and
`test.odile.fyi` to the new IPv4 the script printed. Leave **alone**:
`demo.odile.fyi` (the demo tenant), `www`, and every hostname that starts with
`odl-` (the tenant guests). Wait five minutes.

---

## If something fails

Open `~/odile-recovery.log` on the new server and send it to
**help@odile.fyi**. The last line that starts with `==` names the step that
stopped.

The technical appendix is `docs/runbooks/backup-hq.md` §5A.

## What is in this folder

| File | What it is |
|---|---|
| `README.md` | this page |
| `recovery.env` | the secrets: backup password, R2 token, Cloudflare token, zone |
| `recover.sh` | the one command that performs the restore |
| `manifest.json` | the Hermes commit, old IPv4 and DNS names known when the kit was made |
| `CHECKSUMS.txt` | SHA-256 of the files, so you can check the kit was not changed |

The **facts** the restore needs — the Hermes commit, the old IPv4 and the DNS
names — are written by the nightly backup into the snapshot's
`manifest.json`. This folder is only the secrets and the script; it does not
have to be refreshed when HQ changes. The kit's own `manifest.json` is the
fallback for a snapshot taken before the backup wrote those facts.

**Never edit `recovery.env` unless a token was rotated. Never send it to
anyone.**
