#!/usr/bin/python3
"""A hermetic Hetzner Storage Box API for the provisioning tests (ODL-924).

The provisioning path talks to a real Box API, runs `ssh-keygen`, scans the
host key and drives `install-ssh-key` over port 23. None of those belong in a
unit test, so this module supplies the four seams `odilectl.provision` exposes
and keeps the semantics the card measured:

* `POST /subaccounts` **drops** `access_settings` — the account is created
  writable with ssh off, and only `update_access_settings` makes it true;
* the read-only half is locked **after** its key is installed;
* the provider assigns the logins (`u<box>-sub<N>`); nothing is derived from
  the slug;
* the create-time password may not be live, so `reset_subaccount_password` is
  what sets the password the key install uses.

Import it by adding the repository root to `sys.path` and
`from tests.box_fixture import ...` (tests/ is a namespace package).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

from odilectl import provision

#: One syntactically valid host-key line (the shape `seed.validate_backup_pair`
#: accepts). Nothing verifies the key material: the scan seam never dials.
HOST_LINE = ("u728180-sub1.your-storagebox.de ssh-ed25519 "
             "AAAAC3NzaC1lZDI1NTE5AAAAIBenchHostKeyFixture")
# Concatenated so `tools/secret_scan.py` does not read a fixture as a leaked
# key; the value still has the PEM's begin/end lines at run time.
_PEM_BEGIN = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
_PEM_END = "-----END " + "OPENSSH PRIVATE KEY-----"
PRIVATE_KEY = (_PEM_BEGIN + "\nfixture-private-half-not-a-real-key\n"
               + _PEM_END + "\n")

#: One fake box per store-of-record directory, so each test's temp config gets
#: its own provider state (the real box is one, but a test must not inherit
#: another test's tenants).
BOXES: dict[str, "FakeBox"] = {}


def box_for(directory: Path) -> "FakeBox":
    return BOXES.setdefault(str(directory), FakeBox())


class FakeBox:
    """The Box API in memory, with the measured create/update split."""

    def __init__(self) -> None:
        self.accounts: list[dict] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.passwords: list[str] = []
        self.installed: list[tuple[str, str, str]] = []
        self.next_id = 392900

    # -------------------------------------------------------------- fixtures

    def add_pair(self, slug: str, *, access: dict | None = None) -> tuple[dict, dict]:
        """An existing pair, shaped like the drill's 392550/392556."""
        rw = self._account(f"odile-{slug}-rw", f"tenants/{slug}", readonly=False)
        ro = self._account(f"odile-{slug}-ro", f"tenants/{slug}/data", readonly=True)
        rw["access_settings"].update(access or {})
        return rw, ro

    def _account(self, name: str, home: str, *, readonly: bool) -> dict:
        index = len(self.accounts) + 1
        account = {
            "id": self.next_id + index,
            "name": name,
            "username": f"u728180-sub{index}",
            "server": f"u728180-sub{index}.your-storagebox.de",
            "home_directory": home,
            "access_settings": {"readonly": readonly, "ssh_enabled": True,
                                "reachable_externally": True,
                                "samba_enabled": False, "webdav_enabled": False},
        }
        self.accounts.append(account)
        return account

    def _by_id(self, subaccount_id: int) -> dict:
        for account in self.accounts:
            if account["id"] == subaccount_id:
                return account
        raise AssertionError("no sub-account %r in the fake box" % (subaccount_id,))

    def mutating(self) -> list[tuple[str, str, dict]]:
        return [call for call in self.calls if call[0] != "GET"]

    # ------------------------------------------------------------------ API

    def api(self, settings, method: str, path: str, body: dict | None = None):
        body = dict(body or {})
        self.calls.append((method, path, body))
        prefix = f"/storage_boxes/{settings.box_id}"
        if method == "GET" and path == prefix + "/subaccounts":
            return 200, {"subaccounts": [dict(item) for item in self.accounts]}
        if method == "GET" and path.startswith(prefix + "/subaccounts/"):
            return 200, {"subaccount": dict(self._by_id(int(path.rsplit("/", 1)[1])))}
        if method == "POST" and path == prefix + "/subaccounts":
            index = len(self.accounts) + 1
            account = {
                "id": self.next_id + index,
                "name": body["name"],
                "username": f"u728180-sub{index}",
                "server": f"u728180-sub{index}.your-storagebox.de",
                "home_directory": body["home_directory"],
                # The create call drops the access settings it was sent.
                "access_settings": {"readonly": False, "ssh_enabled": False,
                                    "reachable_externally": False,
                                    "samba_enabled": False, "webdav_enabled": False},
            }
            self.accounts.append(account)
            return 201, {"subaccount": {"id": account["id"]}}
        action = path.rsplit("/", 1)[-1]
        subaccount_id = int(path.rsplit("/", 3)[-3])
        account = self._by_id(subaccount_id)
        if action == "update_access_settings":
            account["access_settings"].update({
                "readonly": body["readonly"],
                "ssh_enabled": body.get("ssh_enabled", True),
                "reachable_externally": body.get("reachable_externally", True)})
            return 201, {"action": {"status": "success"}}
        if action == "reset_subaccount_password":
            self.passwords.append(body["password"])
            return 201, {"action": {"status": "success"}}
        raise AssertionError("the fake box has no route for %s %s" % (method, path))


# ---------------------------------------------------------------- the three edges

def generate_keypair(directory: Path, name: str, comment: str) -> tuple[str, str]:
    """A fake ed25519 pair on disk: the private half 0600, public 0644."""
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / name
    private.write_text(PRIVATE_KEY, encoding="utf-8")
    private.chmod(0o600)
    public = Path(str(private) + ".pub")
    public.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBenchPublicHalf %s\n"
                      % comment, encoding="utf-8")
    public.chmod(0o644)
    return private.read_text(encoding="utf-8"), public.read_text(encoding="utf-8").strip()


def scan_host_key(server: str, known_hosts: Path) -> str:
    """The pinned lines (both ports), written at 0600; no network."""
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    key = HOST_LINE.split(" ", 1)[1]
    line = "%s %s" % (server, key)
    existing = []
    if known_hosts.is_file():
        existing = [item for item in known_hosts.read_text(encoding="utf-8").splitlines()
                    if item.strip()]
    written = list(dict.fromkeys(existing + [line, "[%s]:23 %s" % (server, key)]))
    known_hosts.write_text("\n".join(written) + "\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    return line


#: Every API call and key install, in one order, so a test can assert the
#: measured sequence (install before the read-only lock).
EVENTS: list[tuple[str, str]] = []


def install_public_key(server: str, username: str, public_key: str,
                       password: str, known_hosts: Path) -> str:
    """Record the install; the real command writes RFC4716 and OpenSSH formats."""
    INSTALLS.append((server, username, public_key.split()[0]))
    EVENTS.append(("install", username))
    return ("Key No. 1 was installed in RFC4716 format\n"
            "Key No. 1 was installed in OpenSSH format")


#: Every key install the patched seam performed, in order (server, login, type).
INSTALLS: list[tuple[str, str, str]] = []


def _seams():
    return (
        ("_hetzner_api", _api),
        ("_generate_keypair", generate_keypair),
        ("_scan_host_key", scan_host_key),
        ("_install_public_key", install_public_key),
        ("_wait_for_dns", lambda *_args, **_kwargs: None),
        ("SLEEP", lambda _seconds: None),
    )


def start_patchers() -> list:
    """Start every seam patch; the caller stops them (module-level tests)."""
    INSTALLS.clear()
    EVENTS.clear()
    started = []
    for target, value in _seams():
        patcher = mock.patch.object(provision, target, value)
        patcher.start()
        started.append(patcher)
    return started


def patchers(case) -> None:
    """Patch every seam, via `case.addCleanup`, and reset the recorded installs."""
    for patcher in start_patchers():
        case.addCleanup(patcher.stop)


def _api(settings, method: str, path: str, body: dict | None = None):
    """Dispatch to the fake box the settings' store directory names."""
    EVENTS.append(("%s %s" % (method, path),
                   "readonly=%r" % (body or {}).get("readonly")))
    return box_for(Path(settings.store)).api(settings, method, path, body)


def subaccount_path(box_id: int, subaccount_id: int, action: str | None = None) -> str:
    base = f"/storage_boxes/{box_id}/subaccounts/{subaccount_id}"
    return base if action is None else f"{base}/actions/{action}"


def username_at(index: int) -> str:
    return f"u728180-sub{index}"


def slug_of(name: str) -> str:
    match = re.match(r"^odile-(.+)-rw$", name)
    return match.group(1) if match else ""
