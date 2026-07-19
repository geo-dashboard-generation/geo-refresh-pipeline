"""Payload parsing: GeoJSON, JSON with a mapping, CSV, and malformed input."""

from __future__ import annotations

import pytest

from geo_refresh.config import RecordMapping
from geo_refresh.errors import ValidationError
from geo_refresh.formats import parse_csv, parse_geojson, parse_json, parse_payload
from geo_refresh.selectors import select, select_records

POINT_MAPPING = RecordMapping.model_validate(
    {"geometry": {"type": "point", "lon": "lon", "lat": "lat"}}
)


# -- GeoJSON ---------------------------------------------------------------- #


def test_parse_feature_collection() -> None:
    features = parse_geojson(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [13.404954, 52.520008]},
                    "properties": {"city": "Berlin"},
                }
            ],
        }
    )
    assert len(features) == 1
    assert features[0]["properties"]["city"] == "Berlin"


def test_parse_bare_feature_and_bare_geometry() -> None:
    single = parse_geojson(
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": None}
    )
    assert single[0]["properties"] == {}
    geometry_only = parse_geojson({"type": "Point", "coordinates": [9.993682, 53.551086]})
    assert geometry_only[0]["geometry"]["type"] == "Point"


def test_parse_array_of_features() -> None:
    features = parse_geojson(
        [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]}, "properties": {}}]
    )
    assert len(features) == 1


def test_feature_id_is_preserved() -> None:
    features = parse_geojson(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "berlin-1",
                    "geometry": {"type": "Point", "coordinates": [13.4, 52.5]},
                    "properties": {},
                }
            ],
        }
    )
    assert features[0]["id"] == "berlin-1"


def test_feature_collection_without_features_member() -> None:
    with pytest.raises(ValidationError, match="no 'features' member"):
        parse_geojson({"type": "FeatureCollection"})


def test_feature_collection_with_non_list_features() -> None:
    with pytest.raises(ValidationError, match="must be an array"):
        parse_geojson({"type": "FeatureCollection", "features": {"a": 1}})


def test_unrecognised_geojson_type() -> None:
    with pytest.raises(ValidationError, match="unrecognised GeoJSON"):
        parse_geojson({"type": "Topology", "objects": {}})


def test_feature_with_wrong_inner_type() -> None:
    with pytest.raises(ValidationError, match="feature 0 has type 'Point'"):
        parse_geojson({"type": "FeatureCollection", "features": [{"type": "Point"}]})


def test_feature_with_non_object_properties() -> None:
    with pytest.raises(ValidationError, match="'properties' must be an object"):
        parse_geojson(
            {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "geometry": None, "properties": [1, 2]}],
            }
        )


# -- JSON ------------------------------------------------------------------- #


def test_malformed_json_names_the_position() -> None:
    with pytest.raises(ValidationError, match="invalid JSON at line 1"):
        parse_json('{"a": ')


def test_empty_payload_rejected() -> None:
    with pytest.raises(ValidationError, match="payload is empty"):
        parse_json("   ")


def test_undecodable_payload_rejected() -> None:
    with pytest.raises(ValidationError, match="not valid utf-8 text"):
        parse_json(b"\xff\xfe\x00bad")


def test_json_records_mapped_to_points() -> None:
    mapping = RecordMapping.model_validate(
        {
            "records": "$.data.stations",
            "geometry": {"type": "point", "lon": "lon", "lat": "lat"},
            "properties": ["name"],
            "id": "code",
        }
    )
    payload = (
        '{"data": {"stations": ['
        '{"code": "VIE", "name": "Vienna", "lon": 16.373819, "lat": 48.208176}]}}'
    )
    features = parse_payload(payload, "json", mapping)
    assert features[0]["id"] == "VIE"
    assert features[0]["properties"] == {"name": "Vienna"}
    assert features[0]["geometry"]["coordinates"] == [16.373819, 48.208176]


def test_json_embedded_geometry_field() -> None:
    mapping = RecordMapping.model_validate(
        {"geometry": {"type": "geometry", "field": "shape"}, "properties": ["name"]}
    )
    payload = '[{"name": "Zurich", "shape": {"type": "Point", "coordinates": [8.541694, 47.376887]}}]'
    features = parse_payload(payload, "json", mapping)
    assert features[0]["geometry"]["type"] == "Point"


def test_json_embedded_geometry_rejects_garbage() -> None:
    mapping = RecordMapping.model_validate({"geometry": {"type": "geometry", "field": "shape"}})
    with pytest.raises(ValidationError, match="not a valid GeoJSON geometry"):
        parse_payload('[{"shape": {"type": "Blob", "coordinates": [1, 2]}}]', "json", mapping)


def test_json_missing_geometry_field_lists_available_fields() -> None:
    with pytest.raises(ValidationError, match="record 0: longitude field 'lon' is missing"):
        parse_payload('[{"x": 1, "y": 2}]', "json", POINT_MAPPING)


def test_json_non_numeric_coordinate_rejected() -> None:
    with pytest.raises(ValidationError, match="is not a number"):
        parse_payload('[{"lon": "west", "lat": 2}]', "json", POINT_MAPPING)


def test_json_wildcard_properties_excludes_geometry_fields() -> None:
    features = parse_payload('[{"lon": 1, "lat": 2, "name": "x"}]', "json", POINT_MAPPING)
    assert features[0]["properties"] == {"name": "x"}


# -- Selectors -------------------------------------------------------------- #


def test_selector_walks_nested_objects() -> None:
    assert select({"a": {"b": [1, 2, 3]}}, "$.a.b[1]") == 2
    assert select({"a": {"b": 5}}, "a.b") == 5
    assert select({"odd.key": 7}, '$["odd.key"]') == 7


def test_selector_wildcard_over_object_values() -> None:
    assert sorted(select({"a": {"x": 1, "y": 2}}, "$.a[*]")) == [1, 2]


def test_selector_reports_missing_key_with_alternatives() -> None:
    with pytest.raises(ValidationError, match="available keys: alpha, beta"):
        select({"alpha": 1, "beta": 2}, "$.gamma")


def test_selector_rejects_bad_syntax() -> None:
    with pytest.raises(ValidationError, match="cannot parse selector"):
        select({"a": 1}, "$.a[")


def test_select_records_requires_a_list_of_objects() -> None:
    with pytest.raises(ValidationError, match="is a single object"):
        select_records({"a": {"b": 1}}, "$.a")
    with pytest.raises(ValidationError, match="record 0 is a int"):
        select_records({"a": [1]}, "$.a")


# -- CSV -------------------------------------------------------------------- #

CSV_TEXT = "station_id,city,lat,lon,pm25\nDE-BE-0012,Berlin,52.520008,13.404954,11.4\n"


def test_csv_parsed_with_type_coercion() -> None:
    records = parse_csv(CSV_TEXT)
    assert records[0]["pm25"] == 11.4
    assert records[0]["city"] == "Berlin"


def test_csv_preserves_leading_zero_identifiers() -> None:
    records = parse_csv("code,value\n01234,7\n")
    assert records[0]["code"] == "01234"
    assert records[0]["value"] == 7


def test_csv_semicolon_delimiter_is_sniffed() -> None:
    records = parse_csv("a;b;c\n1;2;3\n4;5;6\n")
    assert records[0] == {"a": 1, "b": 2, "c": 3}


def test_csv_booleans_and_nulls() -> None:
    records = parse_csv("flag,empty\ntrue,\n")
    assert records[0]["flag"] is True
    assert records[0]["empty"] is None


def test_csv_without_header_rejected() -> None:
    with pytest.raises(ValidationError, match="no header row|empty"):
        parse_csv("")


def test_csv_empty_column_name_rejected() -> None:
    with pytest.raises(ValidationError, match="empty column name"):
        parse_csv("a,,c\n1,2,3\n")


def test_csv_ragged_row_rejected() -> None:
    with pytest.raises(ValidationError, match="more fields than the header"):
        parse_csv("a,b\n1,2,3\n")


def test_csv_to_features_end_to_end() -> None:
    mapping = RecordMapping.model_validate(
        {
            "geometry": {"type": "point", "lon": "lon", "lat": "lat"},
            "properties": ["station_id", "city", "pm25"],
        }
    )
    features = parse_payload(CSV_TEXT, "csv", mapping)
    assert features[0]["geometry"]["coordinates"] == [13.404954, 52.520008]
    assert features[0]["properties"]["station_id"] == "DE-BE-0012"


def test_format_without_mapping_rejected() -> None:
    with pytest.raises(ValidationError, match="requires a 'mapping:' block"):
        parse_payload("a,b\n1,2\n", "csv", None)
