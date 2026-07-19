"""Turn a fetched payload into a list of GeoJSON features.

Three input shapes are supported:

``geojson``
    A ``FeatureCollection``, a bare ``Feature``, a bare geometry, or a JSON
    array of features.
``json``
    Any JSON document; a ``mapping:`` block says where the records live and how
    to build geometry from each one.
``csv``
    Delimited text with a header row; the same ``mapping:`` block applies, with
    column names in place of JSON fields.

Every failure raises :class:`~geo_refresh.errors.ValidationError` with a message
that names the record index and the offending field, because upstream feeds
break in small ways constantly and "invalid JSON" is not an actionable report.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from .config import PointMapping, RecordMapping, SourceFormat
from .errors import ValidationError
from .geometry import geometry_problem
from .selectors import select_records

Feature = dict[str, Any]


def _decode(payload: bytes | str, encoding: str = "utf-8") -> str:
    if isinstance(payload, str):
        return payload
    try:
        return payload.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValidationError(
            f"payload is not valid {encoding} text: {exc}. If the upstream serves a "
            f"different encoding, set 'encoding:' on the source."
        ) from exc


def parse_json(payload: bytes | str, encoding: str = "utf-8") -> Any:
    """Parse JSON, reporting the line/column of a syntax error."""
    text = _decode(payload, encoding)
    if not text.strip():
        raise ValidationError("payload is empty; expected a JSON document")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[max(0, exc.pos - 40) : exc.pos + 40].replace("\n", "\\n")
        raise ValidationError(
            f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}. "
            f"Near: ...{preview}..."
        ) from exc


def _coerce_scalar(value: str) -> Any:
    """Coerce a CSV cell to int/float/bool/None when it round-trips exactly.

    The round-trip check keeps identifiers such as ``"01234"`` or ``"+44"`` as
    strings instead of silently mangling them.
    """
    stripped = value.strip()
    if stripped == "":
        return None
    lowered = stripped.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered == "null":
        return None
    try:
        as_int = int(stripped)
    except ValueError:
        pass
    else:
        if str(as_int) == stripped:
            return as_int
    try:
        as_float = float(stripped)
    except ValueError:
        return value
    if repr(as_float) == stripped or f"{as_float}" == stripped:
        return as_float
    return value


def parse_csv(payload: bytes | str, encoding: str = "utf-8") -> list[dict[str, Any]]:
    """Parse delimited text with a header row into a list of records.

    The delimiter is sniffed from the header line, falling back to a comma.
    """
    text = _decode(payload, encoding).lstrip("﻿")
    if not text.strip():
        raise ValidationError("payload is empty; expected CSV with a header row")
    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValidationError("CSV payload has no header row")
    if any(name is None or name.strip() == "" for name in reader.fieldnames):
        raise ValidationError(
            f"CSV header contains an empty column name: {reader.fieldnames!r}"
        )
    records: list[dict[str, Any]] = []
    for line_number, row in enumerate(reader, start=2):
        if None in row:
            raise ValidationError(
                f"CSV line {line_number} has more fields than the header "
                f"({len(reader.fieldnames)} columns expected)"
            )
        records.append(
            {
                key: _coerce_scalar(value) if isinstance(value, str) else value
                for key, value in row.items()
            }
        )
    return records


def _require_field(record: dict[str, Any], field: str, index: int, role: str) -> Any:
    if field not in record:
        available = ", ".join(sorted(map(str, record))[:10]) or "<no fields>"
        raise ValidationError(
            f"record {index}: {role} field {field!r} is missing; available fields: {available}"
        )
    return record[field]


def _as_float(value: Any, field: str, index: int) -> float:
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"record {index}: field {field!r} is not a number ({value!r})")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"record {index}: field {field!r} is not a number ({value!r})"
        ) from exc


def records_to_features(
    records: list[dict[str, Any]], mapping: RecordMapping
) -> list[Feature]:
    """Build GeoJSON features from plain records using ``mapping``."""
    features: list[Feature] = []
    geometry_fields: set[str] = set()
    if isinstance(mapping.geometry, PointMapping):
        geometry_fields = {mapping.geometry.lon, mapping.geometry.lat}
    else:
        geometry_fields = {mapping.geometry.field}

    for index, record in enumerate(records):
        if isinstance(mapping.geometry, PointMapping):
            lon = _as_float(
                _require_field(record, mapping.geometry.lon, index, "longitude"),
                mapping.geometry.lon,
                index,
            )
            lat = _as_float(
                _require_field(record, mapping.geometry.lat, index, "latitude"),
                mapping.geometry.lat,
                index,
            )
            geometry: Any = {"type": "Point", "coordinates": [lon, lat]}
        else:
            raw = _require_field(record, mapping.geometry.field, index, "geometry")
            if isinstance(raw, str):
                raw = parse_json(raw)
            problem = geometry_problem(raw)
            if problem:
                raise ValidationError(
                    f"record {index}: field {mapping.geometry.field!r} is not a valid "
                    f"GeoJSON geometry ({problem})"
                )
            geometry = raw

        if mapping.properties == "*":
            properties = {
                key: value
                for key, value in record.items()
                if key not in geometry_fields
            }
        else:
            properties = {}
            for name in mapping.properties:
                properties[name] = _require_field(record, name, index, "property")

        feature: Feature = {
            "type": "Feature",
            "geometry": geometry,
            "properties": properties,
        }
        if mapping.id is not None:
            feature["id"] = _require_field(record, mapping.id, index, "id")
        features.append(feature)
    return features


def _normalise_geojson_feature(item: Any, index: int) -> Feature:
    if not isinstance(item, dict):
        raise ValidationError(
            f"feature {index} is a {type(item).__name__}, expected a GeoJSON Feature object"
        )
    kind = item.get("type")
    if kind != "Feature":
        raise ValidationError(
            f"feature {index} has type {kind!r}, expected 'Feature'"
        )
    properties = item.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        raise ValidationError(
            f"feature {index}: 'properties' must be an object or null, "
            f"got {type(properties).__name__}"
        )
    feature: Feature = {
        "type": "Feature",
        "geometry": item.get("geometry"),
        "properties": properties,
    }
    if "id" in item:
        feature["id"] = item["id"]
    return feature


def parse_geojson(document: Any) -> list[Feature]:
    """Extract a feature list from any of the common GeoJSON top-level shapes."""
    if isinstance(document, list):
        return [_normalise_geojson_feature(item, i) for i, item in enumerate(document)]
    if not isinstance(document, dict):
        raise ValidationError(
            f"expected a GeoJSON object, got {type(document).__name__}"
        )
    kind = document.get("type")
    if kind == "FeatureCollection":
        features = document.get("features")
        if features is None:
            raise ValidationError("FeatureCollection has no 'features' member")
        if not isinstance(features, list):
            raise ValidationError(
                f"FeatureCollection 'features' must be an array, "
                f"got {type(features).__name__}"
            )
        return [_normalise_geojson_feature(item, i) for i, item in enumerate(features)]
    if kind == "Feature":
        return [_normalise_geojson_feature(document, 0)]
    if kind in ("Point", "MultiPoint", "LineString", "MultiLineString", "Polygon",
                "MultiPolygon", "GeometryCollection"):
        return [{"type": "Feature", "geometry": document, "properties": {}}]
    raise ValidationError(
        f"unrecognised GeoJSON: top-level 'type' is {kind!r}. Expected "
        f"'FeatureCollection', 'Feature' or a geometry object."
    )


def parse_payload(
    payload: bytes | str,
    fmt: SourceFormat,
    mapping: RecordMapping | None = None,
    *,
    encoding: str = "utf-8",
) -> list[Feature]:
    """Parse a raw payload into features according to ``fmt``.

    Args:
        payload: Raw bytes or text as fetched from the source.
        fmt: One of ``geojson``, ``json`` or ``csv``.
        mapping: Required for ``json`` and ``csv``.
        encoding: Text encoding of ``payload`` when it is bytes.

    Raises:
        ValidationError: On malformed input, with the record index where known.
    """
    if fmt == "geojson":
        return parse_geojson(parse_json(payload, encoding))
    if mapping is None:
        raise ValidationError(f"format '{fmt}' requires a 'mapping:' block")
    if fmt == "json":
        records = select_records(parse_json(payload, encoding), mapping.records)
    elif fmt == "csv":
        records = parse_csv(payload, encoding)
    else:  # pragma: no cover - guarded by config validation
        raise ValidationError(f"unsupported format {fmt!r}")
    return records_to_features(records, mapping)
