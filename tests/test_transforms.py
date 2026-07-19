"""Geometry validation, filter expressions and transform steps."""

from __future__ import annotations

import pytest

from geo_refresh.config import (
    DropInvalidStep,
    FilterStep,
    RenameStep,
    RoundStep,
    SelectStep,
)
from geo_refresh.errors import ConfigError, ValidationError
from geo_refresh.expressions import compile_expression
from geo_refresh.geometry import (
    bounding_box,
    geometry_problem,
    is_valid_geometry,
    round_coordinates,
)
from geo_refresh.transforms import apply_transforms

BERLIN = {"type": "Point", "coordinates": [13.404954, 52.520008]}


def feature(geometry: object = BERLIN, **properties: object) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": dict(properties)}


# -- geometry --------------------------------------------------------------- #


def test_valid_geometries() -> None:
    assert is_valid_geometry(BERLIN)
    assert is_valid_geometry({"type": "LineString", "coordinates": [[0, 0], [1, 1]]})
    assert is_valid_geometry(
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    )
    assert is_valid_geometry(
        {"type": "GeometryCollection", "geometries": [BERLIN]}
    )


@pytest.mark.parametrize(
    ("geometry", "expected"),
    [
        (None, "geometry is null"),
        ({"type": "Blob", "coordinates": [0, 0]}, "unknown geometry type"),
        ({"type": "Point"}, "has no 'coordinates'"),
        ({"type": "Point", "coordinates": [0]}, "2 or 3 numbers"),
        ({"type": "Point", "coordinates": ["a", "b"]}, "must be numbers"),
        ({"type": "Point", "coordinates": [200, 0]}, "outside [-180, 180]"),
        ({"type": "Point", "coordinates": [0, 91]}, "outside [-90, 90]"),
        ({"type": "LineString", "coordinates": []}, "must not be empty"),
        (
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]},
            "at least 4 positions",
        ),
        (
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]},
            "not closed",
        ),
        ({"type": "GeometryCollection", "geometries": []}, "non-empty 'geometries'"),
        ("a string", "must be an object"),
    ],
)
def test_geometry_problems(geometry: object, expected: str) -> None:
    problem = geometry_problem(geometry)
    assert problem is not None and expected in problem


def test_round_coordinates_normalises_negative_zero() -> None:
    assert round_coordinates([-0.000001, 1.234567], 4) == [0.0, 1.2346]


def test_bounding_box() -> None:
    features = [feature(), feature({"type": "Point", "coordinates": [2.352222, 48.856613]})]
    assert bounding_box(features) == [2.352222, 48.856613, 13.404954, 52.520008]
    assert bounding_box([]) is None


# -- expressions ------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("expression", "properties", "expected"),
    [
        ("pop > 100", {"pop": 200}, True),
        ("pop > 100", {"pop": 50}, False),
        ("pop > 100", {}, False),
        ("status == 'active'", {"status": "active"}, True),
        ("country in ['DE', 'AT']", {"country": "AT"}, True),
        ("country not in ['DE', 'AT']", {"country": "CH"}, True),
        ("name != null", {"name": None}, False),
        ("name != null", {"name": "x"}, True),
        ("not closed", {"closed": False}, True),
        ("a > 1 and b < 3", {"a": 2, "b": 2}, True),
        ("a > 1 or b < 3", {"a": 0, "b": 2}, True),
        ("len(name) > 2", {"name": "abc"}, True),
        ("lower(city) == 'berlin'", {"city": "BERLIN"}, True),
        ("startswith(code, 'DE-')", {"code": "DE-BE-01"}, True),
        ("endswith(code, '-01')", {"code": "DE-BE-01"}, True),
        ("contains(tags, 'x')", {"tags": ["x", "y"]}, True),
        ("is_null(missing)", {}, True),
        ("coalesce(a, b) == 5", {"a": None, "b": 5}, True),
        ("abs(delta) > 2", {"delta": -3}, True),
        ("pop / 2 >= 50", {"pop": 100}, True),
        ("1 < pop < 10", {"pop": 5}, True),
        ("(pop if pop else 0) > 1", {"pop": 3}, True),
    ],
)
def test_expression_evaluation(expression: str, properties: dict, expected: bool) -> None:
    assert compile_expression(expression).matches(properties) is expected


def test_expression_lists_referenced_names() -> None:
    assert compile_expression("pop > 1 and len(name) > 2").names() == {"pop", "name"}


def test_expression_rejects_attribute_access() -> None:
    with pytest.raises(ConfigError):
        compile_expression("__import__('os').system('rm -rf /')")
    with pytest.raises(ConfigError, match="unsupported construct"):
        compile_expression("name.upper()")
    with pytest.raises(ConfigError, match="unsupported construct"):
        compile_expression("[x for x in tags]")


def test_expression_rejects_unknown_function() -> None:
    with pytest.raises(ConfigError, match="not\n?\\s*available|not available"):
        compile_expression("eval('1')")


def test_expression_rejects_syntax_error() -> None:
    with pytest.raises(ConfigError, match="is not valid"):
        compile_expression("pop >")


def test_expression_rejects_keyword_arguments() -> None:
    with pytest.raises(ConfigError, match="keyword arguments"):
        compile_expression("len(name=1)")


def test_expression_division_by_zero_is_a_validation_error() -> None:
    with pytest.raises(ValidationError, match="division by zero"):
        compile_expression("1 / n > 1").evaluate({"n": 0})


def test_expression_type_mismatch_is_a_validation_error() -> None:
    with pytest.raises(ValidationError, match="could not be evaluated"):
        compile_expression("a + b").evaluate({"a": "x", "b": 1})


# -- transform steps -------------------------------------------------------- #


def test_filter_step_drops_non_matching_features() -> None:
    features = [feature(pop=10), feature(pop=1000)]
    result, reports = apply_transforms(features, [FilterStep(expression="pop > 100")])
    assert len(result) == 1
    assert reports[0].removed == 1


def test_select_step_keeps_only_named_properties() -> None:
    result, _ = apply_transforms(
        [feature(a=1, b=2, c=3)], [SelectStep(properties=["a", "c", "missing"])]
    )
    assert result[0]["properties"] == {"a": 1, "c": 3}


def test_rename_step_leaves_other_properties_alone() -> None:
    result, _ = apply_transforms([feature(a=1, b=2)], [RenameStep(mapping={"a": "alpha"})])
    assert result[0]["properties"] == {"alpha": 1, "b": 2}


def test_round_step_reduces_precision() -> None:
    result, _ = apply_transforms([feature()], [RoundStep(precision=2)])
    assert result[0]["geometry"]["coordinates"] == [13.4, 52.52]


def test_drop_invalid_step_removes_broken_geometry() -> None:
    features = [feature(), feature(None), feature({"type": "Point", "coordinates": [999, 0]})]
    result, reports = apply_transforms(features, [DropInvalidStep()])
    assert len(result) == 1
    assert "geometry is null" in reports[0].detail


def test_drop_invalid_step_can_be_disabled() -> None:
    result, _ = apply_transforms([feature(None)], [DropInvalidStep(enabled=False)])
    assert len(result) == 1


def test_transforms_do_not_mutate_the_input() -> None:
    original = feature(a=1)
    apply_transforms([original], [SelectStep(properties=["b"]), RoundStep(precision=1)])
    assert original["properties"] == {"a": 1}
    assert original["geometry"]["coordinates"] == [13.404954, 52.520008]


def test_transform_reports_chain_in_order() -> None:
    features = [feature(pop=10, name="a"), feature(pop=1000, name="b")]
    _, reports = apply_transforms(
        features,
        [FilterStep(expression="pop > 100"), SelectStep(properties=["name"]), RoundStep(precision=3)],
    )
    assert [r.step for r in reports] == ["filter", "select", "round"]
    assert reports[0].features_in == 2 and reports[0].features_out == 1
    assert reports[1].features_in == 1
    assert reports[0].to_dict()["removed"] == 1
