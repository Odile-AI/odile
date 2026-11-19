Odile recovery kit — {{slug}}
========================================

Machine:                {{slug}}
Address:                {{ipv4}}
URL:                    {{url}}
Machine ID:             {{instance_id}}
Release:                {{release}}
vCPUs:                  {{vcpus}}
Measurement:            {{measurement}}
Set your sign-in:       {{first_visit_url}}
Token expires:          {{token_expiry}}
Signing key:            SHA-256 {{signing_key_sha256}}
Seed signing key:       SHA-256 {{seed_signing_key_sha256}}
Seed certificate:       SHA-256 {{seed_certificate_sha256}}
Generated:              {{written_at}}
{{#backup_repo_url}}Backup repository:      {{backup_repo_url}}
{{/backup_repo_url}}
Keep this file where you can reach it when the machine is gone. Save it as
odile-{{slug}}-recovery-kit.txt. The recovery file from your first
visit is odile-recovery-{{slug}}.txt; it is a different file, and it
holds the backup password.

A recovery code and a recovery file are the way back: the code resets your
sign-in, the file opens your backup. Your first visit gives you both; keep
them off the machine. While the machine is still running, the agent can show
it again.

Before you set your sign-in
---------------------------
Open {{url}}/verify.

Check the machine before you trust it: the verify page shows PASS or FAIL,
and you set your sign-in only after PASS. Compare the Measurement line at
the top of this file with the one the page shows and the one in your ready
email.

The first visit
---------------
Open the Set your sign-in link in your ready email. The same URL is printed
at the top of this file. The link expires {{token_expiry_prose}}.
Set the password that opens your workspace: at least 16 characters. Your
Odile sign-in is set on your machine at first visit and hashed there. We
never see it. It is the only app-layer gate: no 2FA in V0.1.

The link carries a single-use setup token. If the link has expired or was
already opened, write to help@odile.fyi for a new one.

The machine
-----------
The disk unlocks only on the boot you verify. Change the image, and it stays
locked.

After a reboot, it starts itself. The seal is the key.

Your two ways back
------------------
The recovery code resets your sign-in. Open
{{url}}/first-visit/reset
and enter the code you saved from the first visit. The code is shown once,
on the first visit. After a reset the old code stops working, and a new one
is shown. The same code fetches your recovery file at
{{url}}/first-visit/recovery; using it there rotates it too.

Your account password is not reset on the machine. It resets at your account
site — the address your claim link came from. Open that address and add
/forgot. That reset works while the machine is down; the recovery code needs
the machine up.

The recovery file opens your backup. Download your copy on the first visit
and keep it off the machine: it is your only copy of the backup password. It
holds the backup password and this machine's certificate fingerprint. The
machine keeps its own copy, so while the machine is still running the agent
can show it again.

If the machine dies
-------------------
Write to help@odile.fyi and ask for a rebuild. Your slug is yours for the
life of the tenant, so the URL and the recovery file's name stay the same.
Ask the agent on the new machine to restore the backup with your recovery
file.

A restore keeps the rebuilt machine's own /etc/odile: the recovery code you
were shown after the rebuild is the one that works, not the one in the
backup.

The encrypted backup is kept 30 days after cancellation, then deleted. Odile
can delete it, not read it.

If something is wrong
---------------------
Ask the agent to run odile-diag and email the bundle to help@odile.fyi. If
that is not enough, ask it to run odile-support grant --hours 24; it prints
the expiry and the revoke command, and every session is logged. A grant is a
handover, not a peek. Expiry and revoke block new logins. An open session
keeps running until it exits or you kill it.

The signing keys
----------------
The app manifest and the release are signed with Odile's Ed25519 release key.
The public key is in your recovery kit and at
https://odile.fyi/odile-signing-key.pub. This is an operator signature, not a
hardware measurement. The SHA-256 of the key file is the Signing key line at
the top of this file.

Your machine's own configuration is signed with a second key, the seed key, and
the release key certifies it: the certificate below is the release key's
signature over the seed key. The chain is release key, then seed key, then the
seed. The Seed signing key and Seed certificate lines at the top of this file
are the SHA-256 of each.

Release signing key
-------------------
{{signing_public_key}}

Seed signing key
----------------
{{seed_signing_public_key}}

Seed certificate (base64)
-------------------------
{{seed_certificate}}
