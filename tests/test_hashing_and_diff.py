"""Content hashing stability and the add/remove/modify diff."""

from __future__ import annotations

import pytest

from geo_refresh.diffing import build_index, compute_diff, feature_key
from geo_refresh.errors import ValidationError
from geo_refresh.hashing import (
    HASH_ALGORITHM,
    canonical_json,
    collection_hash,
    feature_hash,
    hashes_equal,
)


def feature(identifier: str, lon: float = 13.4, lat: float = 52.5, **extra: object) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"id": identifier, **extra},
    }


# -- hashing ---------------------------------------------------------------- #


def test_hash_is_stable_under_property_key_reordering() -> None:
    a = {"type": "Feature", "geometry": None, "properties": {"a": 1, "b": 2, "c": 3}}
    b = {"type": "Feature", "geometry": None, "properties": {"c": 3, "a": 1, "b": 2}}
    assert feature_hash(a) == feature_hash(b)


def test_hash_is_stable_under_nested_key_reordering() -> None:
    a = {"type": "Feature", "geometry": None, "properties": {"meta": {"x": 1, "y": 2}}}
    b = {"type": "Feature", "geometry": None, "properties": {"meta": {"y": 2, "x": 1}}}
    assert feature_hash(a) == feature_hash(b)


def test_collection_hash_is_stable_under_feature_reordering() -> None:
    features = [feature("a"), feature("b"), feature("c")]
    assert collection_hash(features) == collection_hash(list(reversed(features)))


def test_collection_hash_counts_duplicates() -> None:
    one = [feature("a")]
    two = [feature("a"), feature("a")]
    assert collection_hash(one) != collection_hash(two)


def test_collection_hash_changes_with_content() -> None:
    assert collection_hash([feature("a")]) != collection_hash([feature("a", lat=52.6)])
    assert collection_hash([feature("a")]) != collection_hash([feature("a", extra=1)])


def test_collection_hash_is_prefixed_with_the_algorithm() -> None:
    assert collection_hash([]).startswith(f"{HASH_ALGORITHM}:")


def test_integral_floats_and_ints_hash_the_same() -> None:
    a = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]}, "properties": {}}
    b = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
        "properties": {},
    }
    assert feature_hash(a) == feature_hash(b)


def test_feature_id_participates_in_the_hash() -> None:
    base = {"type": "Feature", "geometry": None, "properties": {}}
    assert feature_hash(base) != feature_hash({**base, "id": "x"})


def test_canonical_json_sorts_and_compacts() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def test_non_finite_numbers_are_rejected() -> None:
    with pytest.raises(ValidationError, match="non-finite"):
        canonical_json({"x": float("nan")})


def test_unhashable_types_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must be JSON types"):
        canonical_json({"x": {1, 2}})


def test_hashes_equal_treats_missing_as_different() -> None:
    assert hashes_equal("a", "a")
    assert not hashes_equal("a", "b")
    assert not hashes_equal(None, "a")
    assert not hashes_equal("a", None)


# -- diff keys -------------------------------------------------------------- #


def test_feature_key_prefers_the_configured_property() -> None:
    assert feature_key(feature("x"), "id") == "x"


def test_feature_key_falls_back_to_the_top_level_id() -> None:
    assert feature_key({"type": "Feature", "id": 7, "properties": {}}, None) == "7"
    assert feature_key({"type": "Feature", "properties": {}}, None) is None


def test_build_index_counts_duplicate_keys() -> None:
    index, duplicates = build_index([feature("a"), feature("a", lat=1.0)], "id")
    assert duplicates == 1
    assert len(index) == 1


# -- diffing ---------------------------------------------------------------- #


def test_first_run_reports_everything_as_added() -> None:
    summary = compute_diff([feature("a"), feature("b")], None, id_property="id")
    assert summary.first_run is True
    assert summary.added == 2 and summary.removed == 0 and summary.modified == 0
    assert summary.describe() == "first run, 2 features"


def test_unchanged_collection_reports_no_change() -> None:
    features = [feature("a"), feature("b")]
    previous, _ = build_index(features, "id")
    summary = compute_diff(features, previous, id_property="id")
    assert not summary.changed
    assert summary.unchanged == 2
    assert summary.describe() == "no change (2 features)"


def test_added_removed_and_modified_are_counted_separately() -> None:
    previous, _ = build_index([feature("a"), feature("b"), feature("c")], "id")
    current = [feature("a"), feature("b", lat=48.2), feature("d")]
    summary = compute_diff(current, previous, id_property="id")
    assert summary.added == 1
    assert summary.removed == 1
    assert summary.modified == 1
    assert summary.unchanged == 1
    assert summary.added_ids == ["d"]
    assert summary.removed_ids == ["c"]
    assert summary.modified_ids == ["b"]
    assert summary.describe() == "+1 / -1 / ~1 (of 3)"


def test_property_only_change_counts_as_modified() -> None:
    previous, _ = build_index([feature("a", pm25=11.4)], "id")
    summary = compute_diff([feature("a", pm25=12.0)], previous, id_property="id")
    assert summary.modified == 1 and summary.added == 0 and summary.removed == 0


def test_unkeyed_diff_falls_back_to_multiset_comparison() -> None:
    unkeyed = [
        {"type": "Feature", "geometry": None, "properties": {"n": 1}},
        {"type": "Feature", "geometry": None, "properties": {"n": 2}},
    ]
    previous, _ = build_index([feature("a"), feature("b")], "id")
    summary = compute_diff(unkeyed, previous, id_property=None)
    assert summary.keyed is False
    assert summary.modified == 0
    assert summary.added == 2 and summary.removed == 2


def test_unkeyed_diff_detects_partial_overlap() -> None:
    kept = {"type": "Feature", "geometry": None, "properties": {"n": 1}}
    previous = {"k1": feature_hash(kept)}
    summary = compute_diff(
        [kept, {"type": "Feature", "geometry": None, "properties": {"n": 2}}],
        previous,
        id_property=None,
    )
    assert summary.added == 1 and summary.removed == 0 and summary.unchanged == 1


def test_diff_sample_ids_are_capped() -> None:
    current = [feature(f"f{i}") for i in range(50)]
    summary = compute_diff(current, {}, id_property="id")
    assert summary.added == 50
    assert len(summary.added_ids) == 10


def test_diff_to_dict_shape() -> None:
    previous, _ = build_index([feature("a")], "id")
    payload = compute_diff([feature("b")], previous, id_property="id").to_dict()
    assert payload["added"] == 1 and payload["removed"] == 1
    assert payload["id_property"] == "id"
    assert set(payload["sample"]) == {"added", "removed", "modified"}


def test_unkeyed_diff_omits_the_sample_block() -> None:
    payload = compute_diff(
        [{"type": "Feature", "geometry": None, "properties": {}}], {}, id_property=None
    ).to_dict()
    assert "sample" not in payload
