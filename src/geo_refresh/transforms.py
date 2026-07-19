"""Feature transform steps.

Each step is a pure function from a feature list to a new feature list; nothing
is mutated in place, so a failing step never leaves a half-transformed
collection behind. Steps run in the order they appear in the config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .config import (
    DropInvalidStep,
    FilterStep,
    RenameStep,
    RoundStep,
    SelectStep,
    TransformStep,
)
from .expressions import compile_expression
from .geometry import geometry_problem, round_geometry

Feature = dict[str, Any]


@dataclass(frozen=True)
class TransformReport:
    """What one transform step did, for logging and the run summary."""

    step: str
    detail: str
    features_in: int
    features_out: int

    @property
    def removed(self) -> int:
        """How many features the step dropped."""
        return max(0, self.features_in - self.features_out)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "step": self.step,
            "detail": self.detail,
            "features_in": self.features_in,
            "features_out": self.features_out,
            "removed": self.removed,
        }


def _copy(feature: Feature) -> Feature:
    result = dict(feature)
    result["properties"] = dict(feature.get("properties") or {})
    return result


def apply_filter(features: Sequence[Feature], step: FilterStep) -> list[Feature]:
    """Keep features whose properties satisfy ``step.expression``."""
    expression = compile_expression(step.expression)
    return [f for f in features if expression.matches(f.get("properties") or {})]


def apply_select(features: Sequence[Feature], step: SelectStep) -> list[Feature]:
    """Keep only the named properties. Missing names are simply absent."""
    wanted = list(step.properties)
    result: list[Feature] = []
    for feature in features:
        properties = feature.get("properties") or {}
        new = _copy(feature)
        new["properties"] = {k: properties[k] for k in wanted if k in properties}
        result.append(new)
    return result


def apply_rename(features: Sequence[Feature], step: RenameStep) -> list[Feature]:
    """Rename properties, leaving unlisted ones untouched."""
    mapping = step.mapping
    result: list[Feature] = []
    for feature in features:
        properties = feature.get("properties") or {}
        new = _copy(feature)
        new["properties"] = {mapping.get(k, k): v for k, v in properties.items()}
        result.append(new)
    return result


def apply_round(features: Sequence[Feature], step: RoundStep) -> list[Feature]:
    """Round all coordinates to ``step.precision`` decimal places."""
    result: list[Feature] = []
    for feature in features:
        new = _copy(feature)
        new["geometry"] = round_geometry(feature.get("geometry"), step.precision)
        result.append(new)
    return result


def apply_drop_invalid(
    features: Sequence[Feature], step: DropInvalidStep
) -> tuple[list[Feature], list[str]]:
    """Drop structurally invalid geometries, returning the reasons dropped."""
    if not step.enabled:
        return list(features), []
    kept: list[Feature] = []
    reasons: list[str] = []
    for index, feature in enumerate(features):
        problem = geometry_problem(feature.get("geometry"))
        if problem is None:
            kept.append(feature)
        else:
            reasons.append(f"feature {index}: {problem}")
    return kept, reasons


def apply_transforms(
    features: Sequence[Feature], steps: Sequence[TransformStep]
) -> tuple[list[Feature], list[TransformReport]]:
    """Run every step in order.

    Returns:
        The transformed features and a per-step report.
    """
    current = list(features)
    reports: list[TransformReport] = []
    for step in steps:
        before = len(current)
        if isinstance(step, FilterStep):
            current = apply_filter(current, step)
            detail = step.expression
        elif isinstance(step, SelectStep):
            current = apply_select(current, step)
            detail = ", ".join(step.properties)
        elif isinstance(step, RenameStep):
            current = apply_rename(current, step)
            detail = ", ".join(f"{k}->{v}" for k, v in step.mapping.items())
        elif isinstance(step, RoundStep):
            current = apply_round(current, step)
            detail = f"{step.precision} decimal places"
        elif isinstance(step, DropInvalidStep):
            current, reasons = apply_drop_invalid(current, step)
            detail = "; ".join(reasons[:3]) if reasons else "no invalid geometries"
            if len(reasons) > 3:
                detail += f" (+{len(reasons) - 3} more)"
        else:  # pragma: no cover - discriminated union covers every case
            raise TypeError(f"unknown transform step: {step!r}")
        reports.append(TransformReport(step.op, detail, before, len(current)))
    return current, reports
