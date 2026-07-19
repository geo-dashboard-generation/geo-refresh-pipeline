"""Environment variable substitution for configuration strings.

Supports ``${NAME}`` and ``${NAME:-default}``. A literal ``$$`` escapes to a
single ``$``. Substitution happens when a source is *used*, not when the config
is parsed, so ``geo-refresh validate`` works on a machine without the secrets.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping

from .errors import ConfigError

_PATTERN = re.compile(
    r"""
    \$\$                                  # escaped dollar
    | \$\{ (?P<name>[A-Za-z_][A-Za-z0-9_]*)
        (?: :- (?P<default>[^}]*) )?
      \}
    """,
    re.VERBOSE,
)


def expand(value: str, env: Mapping[str, str] | None = None, *, where: str = "config") -> str:
    """Expand ``${VAR}`` references in ``value``.

    Args:
        value: The raw string, which may contain zero or more references.
        env: Environment mapping to read from. Defaults to ``os.environ``.
        where: Human-readable context used in the error message.

    Raises:
        ConfigError: If a referenced variable is unset and has no default.
    """
    source = os.environ if env is None else env
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        name = match.group("name")
        default = match.group("default")
        if name in source:
            return source[name]
        if default is not None:
            return default
        missing.append(name)
        return ""

    result = _PATTERN.sub(_replace, value)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ConfigError(
            f"{where} references environment variable(s) that are not set: {names}. "
            f"Export them, or give a fallback with ${{NAME:-default}}."
        )
    return result


def expand_tree(value: Any, env: Mapping[str, str] | None = None, *, where: str = "config") -> Any:
    """Recursively expand every string inside dicts/lists/scalars."""
    if isinstance(value, str):
        return expand(value, env, where=where)
    if isinstance(value, dict):
        return {k: expand_tree(v, env, where=where) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_tree(v, env, where=where) for v in value]
    return value
