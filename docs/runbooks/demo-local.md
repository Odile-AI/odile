# The local demo — `make demo`

**Card:** ODL-320 · **Surface:** `demo/local/` · **Screenshots:**
`demo/local/shots/demo-1440.png`, `demo/local/shots/demo-390.png`,
`demo/local/shots/demo-390-long-name.png` (the 64-character file-name state)

A judge who has never seen Odile can clone the repository and reach the
workspace surface without installing Hermes, without a NEAR key and without a
network. One standard-library Python process serves it on loopback; the page is
plain HTML and form posts, with no JavaScript, no session state and no
third-party request.

It is a reproduction of the surface, not a second product. The chat answer is
canned and names the model route; the shell runs on the judge's own computer;
files and the job log live in a directory on disk so a restart keeps them.

## 1. Run it

```sh
make demo
```

The server prints the URL, the user and a freshly generated password:

```text
odile-demo: url http://127.0.0.1:8000/
odile-demo: user odile
odile-demo: password <generated>
odile-demo: state /…/demo/local/state
odile-demo: heartbeat 60s
odile-demo: serving; Ctrl-C stops it
```

Open the URL and use the printed password in the browser's HTTP Basic prompt.
The page carries the fixed sentence:

> local demo of the Odile workspace; the hosted demo runs the same surface on
> Odile's measured machine

Ctrl-C stops the process cleanly (`odile-demo: stopped`, exit 0). Nothing is
left running and nothing is installed.

| Variable | Default | Meaning |
|---|---|---|
| `DEMO_PORT` (make) / `--port` | `8000` | Loopback port |
| `DEMO_STATE` (make) / `--state` | `demo/local/state` | Workspace, job log and demo state |
| `ODILE_DEMO_PASSWORD` | generated | Fixed password instead of a generated one |
| `--heartbeat-seconds` | `60` | Job interval; the tests shorten it |

`ODILE_DEMO_PASSWORD` is read from the environment, never from the command
line, so it does not appear in `ps`.

## 2. Check it without a browser

```sh
make demo-check
```

This is the acceptance check a clone runs and what `tests/test_local_demo.py`
covers at full speed. It starts the real `server.py`, speaks HTTP to it,
restarts it against the same state directory, and exits 0 only after:

- `GET /` without the password answers `401` with a `WWW-Authenticate: Basic` challenge;
- a wrong password answers `401`, and the printed password answers `200`;
- the page carries the exact sentence above;
- the page sets no state-setting response header;
- one chat post returns exactly one canned answer naming
  `deepseek-ai/DeepSeek-V4.1-Flash-1017` at `cloud-api.near.ai`;
- one shell post runs `printf 'shell-ok-%s\n' "$((6*7))"` in the workspace and
  returns `shell-ok-42` with exit 0;
- one file post writes `proof.txt`, and the file and its contents are still on
  the page after the server is stopped and started again;
- Ctrl-C stops the server with exit 0 and no traceback;
- a new heartbeat line appears within the 60-second job interval (the check
  prints the observed elapsed time);
- the exact sentence is still on the page after the restart.

Observed on a fresh clone of the branch (7 November 2026):

```text
demo-check: ok 401 without the password
…
demo-check: ok the job fires within 60 seconds
demo-check: heartbeat at 60.0s after the restart (2026-11-07T04:33:01Z heartbeat — local demo job fired)
demo-check: PASS 16 checks in 60.3s
```

The check needs no browser and no network. It takes about a minute because the
job interval is the real 60 seconds; `tests/test_local_demo.py` runs the same
behaviours with a sub-second interval inside `make check`.

## 3. The fresh-clone proof

```sh
git clone . /tmp/odile-demo
make -C /tmp/odile-demo demo-check
```

The clone contains no state: `demo/local/state/` is git-ignored and created at
first boot. Run `make check` in the clone too; it includes
`tests/test_local_demo.py`, the compile pass, shellcheck, the claims lint and
the brand lint.

## 4. What the page keeps

```text
demo/local/state/
├── heartbeat.log       the job appends one line at start and every 60 seconds
└── workspace/          files written through the page; the shell's cwd
```

Both survive a server restart. The in-memory chat and shell transcripts do
not; they are the current session, not the machine.

To inspect what a reset would remove, `make demo-reset` is a dry run:

```sh
make demo-reset            # "would remove N files (… bytes) under …"
make demo-reset APPLY=1    # removes the directory
```

Running the dry run and the apply again on a missing directory prints
`nothing to remove`; both are idempotent.

## 5. What it is not

- **Not the hosted machine.** No Hermes, no SEV-SNP report, no NEAR call, no
  network, no TLS. The page says this in the rail and the chat answer says the
  local answer is canned.
- **The shell is real.** It runs as the invoking user, in
  `demo/local/state/workspace`, with a five-second timeout and the process
  group killed on timeout. The Basic password is the only gate; keep it on
  `127.0.0.1` and do not expose the port.
- **One workspace, one user.** There is no session: every request sees the same
  in-memory transcript. That is deliberate for a local demo.
- **Not a service.** Nothing is installed, no systemd unit is created and no
  files are written outside the state directory.

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `cannot listen on 127.0.0.1:8000` | Another process owns the port. `make demo DEMO_PORT=8001`. |
| Browser asks for a password again | HTTP Basic has no session state; enter the printed user `odile` and password. Restarting `make demo` generates a new one. |
| `make demo-check` fails on the heartbeat | The 60-second interval was missed or the clock stalled; rerun. `--heartbeat-seconds` exists for the fast tests, not for the acceptance check. |
| Port changed in `make demo-check` | The check binds an ephemeral loopback port and parses it from the printed URL, so checks can run beside a live `make demo`. |
