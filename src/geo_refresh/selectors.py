"""A small, dependency-free JSONPath subset used to locate record lists.

Only the constructs that matter for "where in this API response is the array of
things" are supported, which keeps the behaviour predictable and avoids pulling
in a full JSONPath engine:

===========================  ==================================================
``$``                        the document root
``$.a.b``                    object member access
``a.b``                      leading ``$.`` is optional
``$.a[0]``                   list index
``$.a[*]``                   every element of a list
``$["odd.key"]``             quoted member access for keys containing dots
===========================  ==================================================

:func:`select_records` applies a selector and insists that the result is a list
of objects, which is what every downstream stage expects.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ValidationError

_TOKEN_RE = re.compile(
    r"""
      \.(?P<member>[^.\[\]]+)
    | \[\s*(?P<index>-?\d+)\s*\]
    | \[\s*(?P<wildcard>\*)\s*\]
    | \[\s*(?P<quote>['"])(?P<quoted>(?:\\.|[^\\])*?)(?P=quote)\s*\]
    """,
    re.VERBOSE,
)


def _tokenize(selector: str) -> list[tuple[str, Any]]:
    text = selector.strip()
    if not text:
        return []
    if text == "$":
        return []
    if not text.startswith("$"):
        text = "$." + text.lstrip(".")
    position = 1
    tokens: list[tuple[str, Any]] = []
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if not match:
            raise ValidationError(
                f"cannot parse selector {selector!r} at position {position}: "
                f"expected '.name', '[0]', '[*]' or '[\"name\"]'"
            )
        if match.group("member") is not None:
            tokens.append(("member", match.group("member")))
        elif match.group("index") is not None:
            tokens.append(("index", int(match.group("index"))))
        elif match.group("wildcard") is not None:
            tokens.append(("wildcard", None))
        else:
            tokens.append(("member", match.group("quoted").replace("\\", "")))
        position = match.end()
    return tokens


def select(document: Any, selector: str) -> Any:
    """Evaluate ``selector`` against ``document``.

    A wildcard produces a list of every match. Missing members raise, because a
    silently empty result would look identical to an upstream that legitimately
    returned nothing.

    Raises:
        ValidationError: If the selector is malformed or does not match.
    """
    tokens = _tokenize(selector)
    current: Any = document
    walked = "$"
    for kind, value in tokens:
        if kind == "member":
            walked += f".{value}"
            if not isinstance(current, dict):
                raise ValidationError(
                    f"selector {selector!r}: expected an object at {walked!r} "
                    f"but found {type(current).__name__}"
                )
            if value not in current:
                available = ", ".join(sorted(map(str, current))[:8]) or "<none>"
                raise ValidationError(
                    f"selector {selector!r}: key {value!r} not found at {walked!r}; "
                    f"available keys: {available}"
                )
            current = current[value]
        elif kind == "index":
            walked += f"[{value}]"
            if not isinstance(current, list):
                raise ValidationError(
                    f"selector {selector!r}: expected a list at {walked!r} "
                    f"but found {type(current).__name__}"
                )
            try:
                current = current[value]
            except IndexError as exc:
                raise ValidationError(
                    f"selector {selector!r}: index {value} out of range at {walked!r} "
                    f"(length {len(current)})"
                ) from exc
        else:  # wildcard
            walked += "[*]"
            if isinstance(current, list):
                current = list(current)
            elif isinstance(current, dict):
                current = list(current.values())
            else:
                raise ValidationError(
                    f"selector {selector!r}: '[*]' needs a list or object at {walked!r} "
                    f"but found {type(current).__name__}"
                )
    return current


def select_records(document: Any, selector: str | None) -> list[dict[str, Any]]:
    """Select a list of record objects, validating the shape.

    Raises:
        ValidationError: If the selection is not a list of objects.
    """
    value = document if selector is None else select(document, selector)
    where = selector or "the document root"
    if isinstance(value, dict):
        raise ValidationError(
            f"{where} is a single object, not a list of records; point the "
            f"'records:' selector at the array, e.g. '$.data.stations'"
        )
    if not isinstance(value, list):
        raise ValidationError(
            f"{where} must select a list of records, got {type(value).__name__}"
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValidationError(
                f"{where}: record {index} is a {type(item).__name__}, expected an object"
            )
    return value
