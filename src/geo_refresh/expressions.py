"""A small, safe expression language for filtering features by property.

Expressions look like Python, because that is what people guess:

.. code-block:: yaml

    - filter: "capacity > 0 and status == 'active'"
    - filter: "country in ['DE', 'AT', 'CH']"
    - filter: "name != null and len(name) > 2"

They are compiled with :mod:`ast` and evaluated against an explicit node
whitelist. There is no attribute access, no subscripting of arbitrary objects,
no imports and no call of anything outside :data:`FUNCTIONS`, so a config file
cannot reach the host through a filter expression.

Bare names resolve to feature properties. An unknown property evaluates to
``None`` rather than raising, so a feed that omits an optional field simply
fails the comparison instead of aborting the run. ``null``/``true``/``false``
are accepted as aliases for ``None``/``True``/``False``.
"""

from __future__ import annotations

import ast
from typing import Any, Callable, Mapping

from .errors import ConfigError, ValidationError

#: Functions callable from a filter expression.
FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": lambda value: 0 if value is None else len(value),
    "lower": lambda value: value.lower() if isinstance(value, str) else value,
    "upper": lambda value: value.upper() if isinstance(value, str) else value,
    "abs": lambda value: abs(value) if isinstance(value, (int, float)) else value,
    "int": lambda value: None if value is None else int(value),
    "float": lambda value: None if value is None else float(value),
    "str": lambda value: "" if value is None else str(value),
    "bool": bool,
    "startswith": lambda value, prefix: isinstance(value, str) and value.startswith(prefix),
    "endswith": lambda value, suffix: isinstance(value, str) and value.endswith(suffix),
    "contains": lambda haystack, needle: needle in haystack if haystack is not None else False,
    "is_null": lambda value: value is None,
    "coalesce": lambda *values: next((v for v in values if v is not None), None),
}

_CONSTANTS: dict[str, Any] = {
    "null": None,
    "none": None,
    "None": None,
    "true": True,
    "True": True,
    "false": False,
    "False": False,
}

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.Constant, ast.Name, ast.Load, ast.Call,
    ast.List, ast.Tuple, ast.Set, ast.IfExp,
    # Allowed only so that the dedicated "keyword arguments" error fires below.
    ast.keyword,
)


class Expression:
    """A compiled, reusable filter expression."""

    __slots__ = ("source", "_tree")

    def __init__(self, source: str) -> None:
        """Compile ``source``.

        Raises:
            ConfigError: If the expression is syntactically invalid or uses a
                construct outside the whitelist.
        """
        self.source = source
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ConfigError(
                f"filter expression {source!r} is not valid: {exc.msg} "
                f"(at offset {exc.offset})"
            ) from exc
        # Two passes: reject unsupported node types first, so that e.g.
        # `name.upper()` is reported as an unsupported attribute access rather
        # than as an unknown function call.
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise ConfigError(
                    f"filter expression {source!r} uses an unsupported construct "
                    f"({type(node).__name__}). Filters may use comparisons, "
                    f"and/or/not, arithmetic, membership tests and these functions: "
                    f"{', '.join(sorted(FUNCTIONS))}."
                )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                    name = getattr(node.func, "id", "<expression>")
                    raise ConfigError(
                        f"filter expression {source!r} calls {name!r}, which is not "
                        f"available. Allowed: {', '.join(sorted(FUNCTIONS))}."
                    )
                if node.keywords:
                    raise ConfigError(
                        f"filter expression {source!r}: keyword arguments are not supported"
                    )
        self._tree = tree.body

    def names(self) -> set[str]:
        """Property names referenced by the expression."""
        return {
            node.id
            for node in ast.walk(self._tree)
            if isinstance(node, ast.Name)
            and node.id not in FUNCTIONS
            and node.id not in _CONSTANTS
        }

    def evaluate(self, properties: Mapping[str, Any]) -> Any:
        """Evaluate against a property mapping.

        Raises:
            ValidationError: If evaluation fails for a reason other than a
                missing property (e.g. comparing a string with a number).
        """
        try:
            return self._eval(self._tree, properties)
        except ValidationError:
            raise
        except TypeError as exc:
            raise ValidationError(
                f"filter expression {self.source!r} could not be evaluated against "
                f"properties {dict(properties)!r}: {exc}"
            ) from exc
        except ZeroDivisionError as exc:
            raise ValidationError(
                f"filter expression {self.source!r}: division by zero"
            ) from exc

    def matches(self, properties: Mapping[str, Any]) -> bool:
        """Evaluate and coerce the result to a boolean."""
        return bool(self.evaluate(properties))

    # -- evaluation ------------------------------------------------------- #

    def _eval(self, node: ast.AST, ctx: Mapping[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in _CONSTANTS:
                return _CONSTANTS[node.id]
            return ctx.get(node.id)
        if isinstance(node, ast.BoolOp):
            values = node.values
            if isinstance(node.op, ast.And):
                result: Any = True
                for value in values:
                    result = self._eval(value, ctx)
                    if not result:
                        return result
                return result
            for value in values:
                result = self._eval(value, ctx)
                if result:
                    return result
            return result
        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand, ctx)
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            return +operand
        if isinstance(node, ast.BinOp):
            left, right = self._eval(node.left, ctx), self._eval(node.right, ctx)
            return _BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, ctx)
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator, ctx)
                if not _compare(operator, left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            branch = node.body if self._eval(node.test, ctx) else node.orelse
            return self._eval(branch, ctx)
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._eval(item, ctx) for item in node.elts]
        if isinstance(node, ast.Set):
            return {self._eval(item, ctx) for item in node.elts}
        if isinstance(node, ast.Call):
            func = FUNCTIONS[node.func.id]  # type: ignore[union-attr]
            args = [self._eval(arg, ctx) for arg in node.args]
            try:
                return func(*args)
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"filter expression {self.source!r}: call to "
                    f"{node.func.id!r} failed: {exc}"  # type: ignore[union-attr]
                ) from exc
        raise ValidationError(  # pragma: no cover - whitelist keeps us out of here
            f"filter expression {self.source!r}: unsupported node {type(node).__name__}"
        )


_BINOPS: dict[type[ast.AST], Callable[[Any, Any], Any]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}


def _compare(operator: ast.AST, left: Any, right: Any) -> bool:
    if isinstance(operator, ast.Eq):
        return bool(left == right)
    if isinstance(operator, ast.NotEq):
        return bool(left != right)
    if isinstance(operator, ast.Is):
        return left is right
    if isinstance(operator, ast.IsNot):
        return left is not right
    if isinstance(operator, ast.In):
        return bool(right is not None and left in right)
    if isinstance(operator, ast.NotIn):
        return bool(right is None or left not in right)
    # Ordering comparisons against a missing property are always False rather
    # than a TypeError, so an optional field does not blow up a whole run.
    if left is None or right is None:
        return False
    if isinstance(operator, ast.Lt):
        return bool(left < right)
    if isinstance(operator, ast.LtE):
        return bool(left <= right)
    if isinstance(operator, ast.Gt):
        return bool(left > right)
    return bool(left >= right)


def compile_expression(source: str) -> Expression:
    """Compile a filter expression. See :class:`Expression`."""
    return Expression(source)
