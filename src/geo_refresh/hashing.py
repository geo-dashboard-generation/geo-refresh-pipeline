"""Order-stable content hashing for feature collections.

The pipeline's whole point is skipping work when nothing really changed, so the
hash has to ignore differences that do not affect the map:

* **Key order.** ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` hash the same,
  because JSON object member order carries no meaning.
* **Feature order.** Many APIs return rows in nondeterministic order. The
  collection hash is built from the *sorted* list of per-feature hashes, so a
  reordered response is not a change. Duplicated features still count, because
  the sorted list is a multiset, not a set.
* **Numeric spelling.** ``1``, ``1.0`` and ``-0.0`` normalise to the same token.

What it deliberately does *not* ignore: property values, geometry coordinates,
feature ids, and the presence or absence of a property.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Sequence

from .errors import ValidationError

Feature = dict[str, Any]

#: Prefix mixed into the collection hash so the algorithm can be versioned.
HASH_ALGORITHM = "sha256-features-v1"


def canonicalise(value: Any) -> Any:
    """Return a JSON-canonical form of ``value``.

    Dict keys are coerced to strings and sorted; integral floats collapse to
    ints; ``-0.0`` becomes ``0``.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValidationError(
                f"cannot hash non-finite number {value!r}; NaN and Infinity are not "
                f"valid JSON and must be filtered out before hashing"
            )
        if value == int(value):
            return int(value)
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(k): canonicalise(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [canonicalise(item) for item in value]
    raise ValidationError(
        f"cannot hash value of type {type(value).__name__}; feature properties must be "
        f"JSON types (string, number, boolean, null, array, object)"
    )


def canonical_json(value: Any) -> str:
    """Serialise ``value`` to its canonical JSON string."""
    return json.dumps(
        canonicalise(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def feature_hash(feature: Feature) -> str:
    """SHA-256 of one canonicalised feature, as hex."""
    payload = {
        "geometry": feature.get("geometry"),
        "properties": feature.get("properties") or {},
    }
    if "id" in feature:
        payload["id"] = feature["id"]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def feature_hashes(features: Iterable[Feature]) -> list[str]:
    """Per-feature hashes, in input order."""
    return [feature_hash(feature) for feature in features]


def collection_hash(features: Sequence[Feature]) -> str:
    """Order-independent content hash of a feature collection.

    Returns:
        A hex digest prefixed with the algorithm id, e.g.
        ``"sha256-features-v1:9f86d0..."``.
    """
    digest = hashlib.sha256()
    digest.update(HASH_ALGORITHM.encode("ascii"))
    digest.update(b"\x00")
    digest.update(str(len(features)).encode("ascii"))
    for item in sorted(feature_hashes(features)):
        digest.update(b"\x00")
        digest.update(item.encode("ascii"))
    return f"{HASH_ALGORITHM}:{digest.hexdigest()}"


def hashes_equal(left: str | None, right: str | None) -> bool:
    """Whether two content hashes represent the same content.

    ``None`` on either side means "unknown", which is never equal — a missing
    previous hash must always be treated as a change.
    """
    if not left or not right:
        return False
    return left == right
