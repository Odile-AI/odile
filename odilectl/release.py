"""`release` — let the machine go while the tenant row stays `destroyed`.

The handover step of `docs/runbooks/recycle.md` §3a (ODL-348). `destroy`
terminates the machine; a recycle keeps it, so the old tenant's row must let go
of it instead. `tenant_state.server_id` and `tenants.server_id` are `UNIQUE`,
and a destroyed row otherwise keeps the id as its zombie marker, so the next
tenant's `provision --apply` died after its writes had started:

    sqlite3.IntegrityError: UNIQUE constraint failed: tenant_state.server_id

`release` clears the machine columns of both tables in one transaction — the
state row and the web row commit together — leaves the row `destroyed` and its
slug intact, and records the machine it let go in `detail.released`, which is
how `reconcile` still reports a destroyed row whose machine lives on. The
machine is not terminated, renamed, powered or otherwise touched here: this
command reads and writes the database only.

Three facts make it safe to type twice: an absent row is success, an already
released row is success, and `--dry-run` (or no `--apply`) prints the same plan
without a write.

    odilectl release odl-7f3a2c --reason 'handover to odl-348b2c' --apply
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from .core import Context, Report, applied, plan
from .state import DEFAULT_STATE_DB, MACHINE_FIELDS, StateStore, UnknownTenant

NAME = "release"
HELP = "Detach a tenant from its machine without terminating it (the handover step)."
EXAMPLE = "odilectl release odl-7f3a2c --reason 'handover to odl-348b2c' --apply"

# The three columns a person looks for first, in the order the report prints.
NAMED = ("server_id", "hostname", "ipv4")


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the tenant's slug (the row to let go of its machine)")
    parser.add_argument(
        "--reason",
        metavar="TEXT",
        help="why the machine is being freed; recorded with the released machine",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="tenant state database (default: the config's state_db)",
    )


def _state_path(ctx: Context, args) -> Path:
    explicit = getattr(args, "db", None)
    if explicit:
        return Path(explicit).expanduser()
    return ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)


def _survey(store: StateStore, slug: str) -> tuple[Any, dict[str, Any], dict | None]:
    """The row, the machine columns it still names, and the web row."""
    try:
        tenant = store.get(slug)
    except UnknownTenant:
        tenant = None
    machine: dict[str, Any] = {}
    if tenant is not None:
        machine = {
            name: getattr(tenant, name)
            for name in MACHINE_FIELDS
            if getattr(tenant, name) is not None
        }
    return tenant, machine, store.web_row(slug)


def _plan_actions(
    slug: str, tenant: Any, machine: dict[str, Any], web: dict | None
) -> list[dict[str, Any]]:
    if tenant is None:
        state = {"action": "state.release", "slug": slug, "result": "no tenant row"}
    else:
        state = {
            "action": "state.release",
            "slug": slug,
            "from": tenant.state,
            "to": "destroyed",
            "cleared": list(machine),
            "released": {name: machine[name] for name in NAMED if name in machine} or None,
            "result": (
                "already released"
                if tenant.state == "destroyed" and not machine
                else "release"
            ),
        }
    if web is None:
        result = "absent"
    elif web.get("server_id") or web.get("ipv4"):
        result = "cleared"
    else:
        result = "already clear"
    return [state, {"action": "web.release", "slug": slug, "cleared": ["server_id", "ipv4"], "result": result}]


def _notes() -> list[str]:
    return [
        "the machine is not terminated: it keeps running at the provider, and "
        "`odilectl provision <next slug> --apply` records it on the next tenant's row",
        "the A record is not this command's: `odilectl dns delete <slug> --apply` "
        "removes it, and its name follows the config's tenant_domain",
        "reconcile still reports the machine as a zombie until a live tenant records "
        "it or it is terminated; the released id is kept in the row's detail",
        "the web `tenants` row keeps its state; only its machine columns are cleared, "
        "in the same transaction as the state row",
    ]


def run(ctx: Context, args) -> Report:
    slug = args.slug
    reason = (getattr(args, "reason", None) or "").strip() or "handover"
    store = StateStore.open(_state_path(ctx, args))
    try:
        tenant, machine, web = _survey(store, slug)
        actions = _plan_actions(slug, tenant, machine, web)
        if not ctx.mutate:
            return plan(
                NAME,
                ctx,
                actions,
                _notes(),
                slug=slug,
                reason=reason,
                state=tenant.state if tenant is not None else None,
                released=actions[0].get("released"),
                web_row=actions[1]["result"],
            )
        result = store.release(slug, reason=reason)
        web_after = store.web_row(slug)
    finally:
        store.close()

    after = result.tenant
    if web_after is None:
        web_result = "absent"
    elif result.web_cleared:
        web_result = "cleared"
    else:
        web_result = "already clear"

    before_state = tenant.state if tenant is not None else None
    lines: list[str] = []
    if after is None:
        lines.append(f"  state:      {slug} has no tenant row; nothing to release")
    elif not result.changed:
        lines.append(f"  state:      {slug} is {after.state} with no machine; already released")
    elif before_state != "destroyed":
        lines.append(f"  state:      {slug}: {before_state} -> destroyed (one event)")
        lines.append("  cleared:    " + (", ".join(machine) or "nothing to clear"))
    else:
        lines.append(f"  state:      {slug} stays destroyed")
        lines.append("  cleared:    " + (", ".join(machine) or "nothing to clear"))
    if result.released:
        named = ", ".join(
            str(result.released[name]) for name in NAMED if result.released.get(name)
        )
        lines.append(f"  released:   {named} (still running at the provider)")
    lines.append(f"  web row:    {web_result} (server_id, ipv4)")
    lines.append(f"  reason:     {reason}")

    return applied(
        NAME,
        ctx,
        lines,
        slug=slug,
        reason=reason,
        state=after.state if after is not None else None,
        before_state=before_state,
        released=result.released,
        cleared=list(machine) if result.changed else [],
        web_row=web_result,
        changed=result.changed,
    )
