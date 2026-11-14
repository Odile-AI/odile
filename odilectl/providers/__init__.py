"""Provider registry: resolve the configured provider without naming a vendor.

`build()` is called lazily — a dry run that never touches a provider must not
need the reseller key. The package discovers its own implementation modules
instead of naming one here: a second provider is one file in this directory,
not an edit in every command, and the vendor's name appears only inside its own
module.
"""

from __future__ import annotations

import importlib
import pkgutil
from functools import lru_cache
from types import ModuleType

from ..errors import ConfigError
from .base import CreateSpec, Machine, Provider

__all__ = ["CreateSpec", "Machine", "Provider", "build", "configured_name"]


@lru_cache(maxsize=1)
def _implementations() -> dict[str, ModuleType]:
    """Every module in this package that exposes `NAME` and `build(config)`."""
    found: dict[str, ModuleType] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        name = getattr(module, "NAME", None)
        builder = getattr(module, "build", None)
        if isinstance(name, str) and callable(builder):
            found[name.strip().lower()] = module
    return found


def configured_name(config) -> str:
    """The provider named by `config`, or the one implementation's own name."""
    implementations = _implementations()
    if not implementations:
        raise ConfigError("no provider implementation in odilectl/providers")
    default = sorted(implementations)[0]
    name = (config.get("provider") or default).strip().lower()
    if name not in implementations:
        known = ", ".join(sorted(implementations))
        raise ConfigError(
            f"config {config.path} names provider {name!r}; known: {known}"
        )
    return name


def build(config) -> Provider:
    """Return the configured provider, built from `config` on first use."""
    return _implementations()[configured_name(config)].build(config)
