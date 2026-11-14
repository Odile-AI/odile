"""Read `/root/.odile/config.yaml` with the standard library only.

The config is the one place the operator's credentials live, so the loader is
deliberately small and strict:

* one flat `key: value` pair per line, `key` in snake_case;
* a line whose first non-space character is `#` is a comment;
* a value may contain `:` and `#` — both are ordinary characters;
* an indented line is an error, so a nested-looking file cannot silently
  become one flat key with a stray value;
* a duplicate key is an error, not last-one-wins;
* the file must not be group- or world-readable. The config holds the
  provider reseller key, the Cloudflare token and the mail relay key.

There is no YAML parser here on purpose. The config does not need lists,
anchors or block scalars, and every YAML feature we do not parse is a way for a
typo to become a default.

The values are never shown by `repr` or by an error message; an error names the
key, not its content.

The copyable template beside this file, `config.example.yaml`, lists every key
the CLI reads: the provider name, the four credential groups the card names,
and the paths.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import ConfigError

DEFAULT_CONFIG_PATH = "/root/.odile/config.yaml"
ENV_CONFIG_PATH = "ODILECTL_CONFIG"

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Config:
    """A parsed config file and the path it came from."""

    path: Path
    values: Mapping[str, str] = field(repr=False)

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        """Read and validate one config file.

        ``path`` defaults to ``$ODILECTL_CONFIG`` and then
        `/root/.odile/config.yaml`. The caller chooses; this class never
        guesses beyond that.
        """
        chosen = Path(path or os.environ.get(ENV_CONFIG_PATH) or DEFAULT_CONFIG_PATH).expanduser()
        try:
            info = chosen.stat()
        except FileNotFoundError:
            raise ConfigError(
                f"no config at {chosen}; create it at mode 0600 or pass --config PATH"
            ) from None
        except OSError as error:
            raise ConfigError(f"cannot read {chosen}: {error.strerror}") from None
        if not chosen.is_file():
            raise ConfigError(f"{chosen} is not a regular file")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise ConfigError(
                f"{chosen} is mode {mode:04o}; a config with credentials must be 0600"
            )
        try:
            text = chosen.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ConfigError(f"{chosen} is not UTF-8 text; rewrite it as UTF-8") from None
        except OSError as error:
            raise ConfigError(f"cannot read {chosen}: {error.strerror}") from None
        return cls(chosen, _parse(text, chosen))

    def get(self, key: str, default: str | None = None) -> str | None:
        """The value for ``key``, or ``default`` when it is absent or empty."""
        value = self.values.get(key)
        if value is None or value == "":
            return default
        return value

    def require(self, key: str) -> str:
        """The value for ``key``; a missing or empty key is a `ConfigError`."""
        value = self.get(key)
        if value is None:
            raise ConfigError(f"config {self.path} has no '{key}' (mode 0600)")
        return value

    def path_value(self, key: str, default: str | os.PathLike[str] | None = None) -> Path | None:
        """A configured filesystem path, with `~` expanded."""
        value = self.get(key)
        if value is None:
            return Path(default).expanduser() if default is not None else None
        return Path(value).expanduser()


def _parse(text: str, path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] in " \t":
            raise ConfigError(
                f"{path}:{lineno}: indented lines are not part of the config; "
                "write one flat 'key: value' per line"
            )
        key, separator, value = raw.partition(":")
        if not separator:
            raise ConfigError(f"{path}:{lineno}: expected 'key: value'")
        key = key.strip()
        if not _KEY_RE.match(key):
            raise ConfigError(
                f"{path}:{lineno}: key {key!r} must be lower-case snake_case"
            )
        if key in values:
            raise ConfigError(f"{path}:{lineno}: duplicate key {key!r}")
        values[key] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
