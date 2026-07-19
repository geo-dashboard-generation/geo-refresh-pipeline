"""GeoJSON geometry validation and coordinate utilities.

Deliberately dependency-free: the pipeline only needs to know whether a
geometry is *structurally* sound (right nesting depth, numeric positions, rings
closed) rather than topologically valid. Anything needing real topology should
run a shapely/geopandas step as a ``build`` output instead.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Nesting depth of the ``coordinates`` member for each simple geometry type.
_COORD_DEPTH: dict[str, int] = {
    "Point": 0,
    "MultiPoint": 1,
    "LineString": 1,
    "MultiLineString": 2,
    "Polygon": 2,
    "MultiPolygon": 3,
}

GEOMETRY_TYPES = frozenset({*_COORD_DEPTH, "GeometryCollection"})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_position(position: Any) -> str | None:
    if not isinstance(position, (list, tuple)):
        return f"position must be an array, got {type(position).__name__}"
    if not 2 <= len(position) <= 3:
        return f"position must have 2 or 3 numbers, got {len(position)}"
    if not all(_is_number(component) for component in position):
        return "position components must be numbers"
    lon, lat = float(position[0]), float(position[1])
    if not -180.0 <= lon <= 180.0:
        return f"longitude {lon} is outside [-180, 180]"
    if not -90.0 <= lat <= 90.0:
        return f"latitude {lat} is outside [-90, 90]"
    return None


def _check_nested(coordinates: Any, depth: int) -> str | None:
    if depth == 0:
        return _check_position(coordinates)
    if not isinstance(coordinates, list):
        return f"expected an array at nesting depth {depth}, got {type(coordinates).__name__}"
    if not coordinates:
        return "coordinate array must not be empty"
    for element in coordinates:
        problem = _check_nested(element, depth - 1)
        if problem:
            return problem
    return None


def geometry_problem(geometry: Any) -> str | None:
    """Return a human-readable problem with ``geometry``, or ``None`` if sound.

    ``None`` geometry (a valid GeoJSON null geometry) is reported as a problem,
    because a feature without a location is not useful on a map.
    """
    if geometry is None:
        return "geometry is null"
    if not isinstance(geometry, dict):
        return f"geometry must be an object, got {type(geometry).__name__}"
    kind = geometry.get("type")
    if kind not in GEOMETRY_TYPES:
        return (
            f"unknown geometry type {kind!r}; expected one of "
            f"{', '.join(sorted(GEOMETRY_TYPES))}"
        )
    if kind == "GeometryCollection":
        members = geometry.get("geometries")
        if not isinstance(members, list) or not members:
            return "GeometryCollection needs a non-empty 'geometries' array"
        for member in members:
            problem = geometry_problem(member)
            if problem:
                return f"GeometryCollection member: {problem}"
        return None
    if "coordinates" not in geometry:
        return f"{kind} geometry has no 'coordinates'"
    problem = _check_nested(geometry["coordinates"], _COORD_DEPTH[kind])
    if problem:
        return f"{kind}: {problem}"
    if kind in ("Polygon", "MultiPolygon"):
        rings = (
            geometry["coordinates"]
            if kind == "Polygon"
            else [ring for polygon in geometry["coordinates"] for ring in polygon]
        )
        for ring in rings:
            if len(ring) < 4:
                return f"{kind}: a linear ring needs at least 4 positions, got {len(ring)}"
            if list(ring[0][:2]) != list(ring[-1][:2]):
                return f"{kind}: linear ring is not closed (first position != last)"
    return None


def is_valid_geometry(geometry: Any) -> bool:
    """Whether ``geometry`` is structurally sound GeoJSON."""
    return geometry_problem(geometry) is None


def round_coordinates(value: Any, precision: int) -> Any:
    """Recursively round every number in a coordinate structure.

    Rounding to 5 decimal places keeps roughly metre-level accuracy while
    typically shrinking a GeoJSON file by a third, and — more importantly for
    this pipeline — stops floating-point noise from upstream showing up as a
    content change on every run.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        rounded = round(float(value), precision)
        # Normalise -0.0 to 0.0 so the hash is stable.
        return rounded + 0.0
    if isinstance(value, (list, tuple)):
        return [round_coordinates(item, precision) for item in value]
    return value


def round_geometry(geometry: Any, precision: int) -> Any:
    """Return a copy of ``geometry`` with coordinates rounded."""
    if not isinstance(geometry, dict):
        return geometry
    result = dict(geometry)
    if "coordinates" in result:
        result["coordinates"] = round_coordinates(result["coordinates"], precision)
    if "geometries" in result and isinstance(result["geometries"], list):
        result["geometries"] = [round_geometry(g, precision) for g in result["geometries"]]
    return result


def iter_positions(geometry: Any) -> Iterable[list[float]]:
    """Yield every ``[lon, lat]`` position in a geometry."""
    if not isinstance(geometry, dict):
        return
    if geometry.get("type") == "GeometryCollection":
        for member in geometry.get("geometries") or []:
            yield from iter_positions(member)
        return
    stack: list[Any] = [geometry.get("coordinates")]
    while stack:
        item = stack.pop()
        if isinstance(item, list) and item and _is_number(item[0]):
            yield [float(item[0]), float(item[1])]
        elif isinstance(item, list):
            stack.extend(item)


def bounding_box(features: Iterable[dict[str, Any]]) -> list[float] | None:
    """Compute ``[min_lon, min_lat, max_lon, max_lat]`` over ``features``."""
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    seen = False
    for feature in features:
        for lon, lat in iter_positions(feature.get("geometry")):
            seen = True
            min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)
            min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
    return [min_lon, min_lat, max_lon, max_lat] if seen else None
