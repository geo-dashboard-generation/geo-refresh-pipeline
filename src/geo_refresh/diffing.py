"""Per-source diff between the previous run and the current one.

The diff answers "what actually changed upstream", which is more useful in a
commit message or a dashboard than "the hash is different". It works off a
compact index of ``feature id -> feature hash`` persisted between runs (see
:mod:`geo_refresh.state`), so the previous full dataset never has to be kept.

Two modes:

**Keyed** (a ``id_property`` is configured, or features carry a top-level
``id``): features are matched by key, giving genuine *added* / *removed* /
*modified* counts.

**Unkeyed**: there is nothing stable to match on, so the diff degrades to a
multiset comparison of feature hashes — *added* and *removed* are still exact,
and *modified* is reported as 0 because a modification is indistinguishable
from one removal plus one addition.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from .hashing import feature_hash

Feature = dict[str, Any]

#: How many example ids to keep in each bucket of the manifest diff.
SAMPLE_LIMIT = 10


@dataclass(frozen=True)
class DiffSummary:
    """Counts (and a few example ids) describing one source's change set."""

    added: int = 0
    removed: int = 0
    modified: int = 0
    unchanged: int = 0
    total: int = 0
    keyed: bool = False
    first_run: bool = False
    id_property: str | None = None
    duplicate_ids: int = 0
    added_ids: list[str] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)
    modified_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Whether anything at all moved."""
        return bool(self.added or self.removed or self.modified)

    def describe(self) -> str:
        """One-line human summary, e.g. ``"+3 / -1 / ~12 (of 480)"``."""
        if self.first_run:
            return f"first run, {self.total} features"
        if not self.changed:
            return f"no change ({self.total} features)"
        parts = [f"+{self.added}", f"-{self.removed}"]
        if self.keyed:
            parts.append(f"~{self.modified}")
        return f"{' / '.join(parts)} (of {self.total})"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, as embedded in the manifest."""
        payload: dict[str, Any] = {
            "added": self.added,
            "removed": self.removed,
            "modified": self.modified,
            "unchanged": self.unchanged,
            "total": self.total,
            "keyed": self.keyed,
            "first_run": self.first_run,
            "id_property": self.id_property,
        }
        if self.duplicate_ids:
            payload["duplicate_ids"] = self.duplicate_ids
        if self.keyed:
            payload["sample"] = {
                "added": self.added_ids,
                "removed": self.removed_ids,
                "modified": self.modified_ids,
            }
        return payload


def feature_key(feature: Feature, id_property: str | None) -> str | None:
    """Return the diff key for ``feature``, or ``None`` if it has none.

    Looks at the configured property first, then the feature's top-level
    ``id``. Keys are stringified so that ``7`` and ``"7"`` match.
    """
    if id_property is not None:
        properties = feature.get("properties") or {}
        if id_property in properties and properties[id_property] is not None:
            return str(properties[id_property])
        return None
    identifier = feature.get("id")
    return None if identifier is None else str(identifier)


def build_index(
    features: Sequence[Feature], id_property: str | None
) -> tuple[dict[str, str], int]:
    """Build the ``key -> feature hash`` index for a feature list.

    Returns:
        The index and the number of features that collided on a key. On a
        collision the last feature wins, matching "the newest row for this id".
    """
    index: dict[str, str] = {}
    duplicates = 0
    for feature in features:
        key = feature_key(feature, id_property)
        if key is None:
            continue
        if key in index:
            duplicates += 1
        index[key] = feature_hash(feature)
    return index, duplicates


def diff_unkeyed(
    previous_hashes: Sequence[str], current_features: Sequence[Feature]
) -> tuple[int, int, int]:
    """Multiset comparison of feature hashes.

    Returns:
        ``(added, removed, unchanged)``.
    """
    before = Counter(previous_hashes)
    after = Counter(feature_hash(f) for f in current_features)
    unchanged = sum((before & after).values())
    return sum((after - before).values()), sum((before - after).values()), unchanged


def compute_diff(
    current_features: Sequence[Feature],
    previous_index: dict[str, str] | None,
    *,
    id_property: str | None = None,
) -> DiffSummary:
    """Diff ``current_features`` against a previous run's index.

    Args:
        current_features: The freshly fetched, transformed features.
        previous_index: The ``key -> hash`` index stored by the previous run,
            or ``None`` on a first run.
        id_property: Property used as the diff key. When ``None``, the
            feature's top-level ``id`` is used if present.

    Returns:
        A :class:`DiffSummary`. On a first run every feature counts as added.
    """
    total = len(current_features)
    keys = [feature_key(f, id_property) for f in current_features]
    keyed = total > 0 and all(key is not None for key in keys)

    if previous_index is None:
        return DiffSummary(
            added=total,
            total=total,
            keyed=keyed,
            first_run=True,
            id_property=id_property,
            added_ids=[k for k in keys if k is not None][:SAMPLE_LIMIT],
        )

    if not keyed:
        added, removed, unchanged = diff_unkeyed(
            list(previous_index.values()), current_features
        )
        return DiffSummary(
            added=added,
            removed=removed,
            modified=0,
            unchanged=unchanged,
            total=total,
            keyed=False,
            id_property=id_property,
        )

    current_index, duplicates = build_index(current_features, id_property)
    added_ids: list[str] = []
    modified_ids: list[str] = []
    unchanged = 0
    for key, digest in current_index.items():
        if key not in previous_index:
            added_ids.append(key)
        elif previous_index[key] != digest:
            modified_ids.append(key)
        else:
            unchanged += 1
    removed_ids = [key for key in previous_index if key not in current_index]

    return DiffSummary(
        added=len(added_ids),
        removed=len(removed_ids),
        modified=len(modified_ids),
        unchanged=unchanged,
        total=total,
        keyed=True,
        id_property=id_property,
        duplicate_ids=duplicates,
        added_ids=sorted(added_ids)[:SAMPLE_LIMIT],
        removed_ids=sorted(removed_ids)[:SAMPLE_LIMIT],
        modified_ids=sorted(modified_ids)[:SAMPLE_LIMIT],
    )
