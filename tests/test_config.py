"""Configuration parsing, defaults, shorthands and validation errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from geo_refresh.config import (
    BuildOutput,
    FileSource,
    GeoJsonOutput,
    HttpSource,
    PointMapping,
    SqlSource,
    load_config,
    parse_config,
    parse_duration,
)
from geo_refresh.errors import ConfigError


def test_parse_duration_accepts_suffixes() -> None:
    assert parse_duration("30s") == 30
    assert parse_duration("15m") == 900
    assert parse_duration("6h") == 21600
    assert parse_duration("7d") == 604800
    assert parse_duration("2w") == 1209600
    assert parse_duration(45) == 45.0


def test_parse_duration_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("soon")


def test_minimal_config_infers_source_type_from_url() -> None:
    config = parse_config({"sources": [{"name": "a", "url": "https://example.org/a.geojson"}]})
    assert isinstance(config.sources[0], HttpSource)
    assert config.sources[0].format == "geojson"


def test_source_type_inferred_from_path_and_query() -> None:
    config = parse_config(
        {
            "sources": [
                {"name": "f", "path": "a.geojson"},
                {
                    "name": "q",
                    "query": "SELECT 1 AS lon, 2 AS lat",
                    "url": "sqlite://",
                    "format": "json",
                    "mapping": {"geometry": {"type": "point", "lon": "lon", "lat": "lat"}},
                },
            ]
        }
    )
    assert isinstance(config.sources[0], FileSource)
    assert isinstance(config.sources[1], SqlSource)


def test_transform_shorthand_expands() -> None:
    config = parse_config(
        {
            "sources": [
                {
                    "name": "a",
                    "path": "a.geojson",
                    "transform": [
                        {"filter": "pop > 1"},
                        {"select": ["pop"]},
                        {"rename": {"pop": "population"}},
                        {"round": 5},
                        {"drop_invalid": True},
                    ],
                }
            ]
        }
    )
    assert [step.op for step in config.sources[0].transform] == [
        "filter",
        "select",
        "rename",
        "round",
        "drop_invalid",
    ]


def test_transform_shorthand_rejects_two_keys() -> None:
    with pytest.raises(ConfigError, match="ambiguous"):
        parse_config(
            {
                "sources": [
                    {"name": "a", "path": "a.geojson", "transform": [{"round": 5, "select": ["x"]}]}
                ]
            }
        )


def test_transform_shorthand_rejects_unknown_step() -> None:
    with pytest.raises(ConfigError, match="unknown transform step"):
        parse_config(
            {"sources": [{"name": "a", "path": "a.geojson", "transform": [{"reproject": 4326}]}]}
        )


def test_output_shorthand_expands() -> None:
    config = parse_config(
        {
            "sources": [{"name": "a", "path": "a.geojson", "outputs": [{"geojson": "out.geojson"}]}],
            "outputs": [{"build": "make site"}],
        }
    )
    assert isinstance(config.sources[0].outputs[0], GeoJsonOutput)
    assert isinstance(config.outputs[0], BuildOutput)
    assert config.outputs[0].command == "make site"


def test_json_format_requires_mapping() -> None:
    with pytest.raises(ConfigError, match="needs a 'mapping:' block"):
        parse_config({"sources": [{"name": "a", "path": "a.json", "format": "json"}]})


def test_geojson_format_rejects_mapping() -> None:
    with pytest.raises(ConfigError, match="only\n?\\s*meaningful|only meaningful"):
        parse_config(
            {
                "sources": [
                    {
                        "name": "a",
                        "path": "a.geojson",
                        "mapping": {"geometry": {"type": "point", "lon": "x", "lat": "y"}},
                    }
                ]
            }
        )


def test_duplicate_source_names_rejected() -> None:
    with pytest.raises(ConfigError, match="duplicate source name"):
        parse_config(
            {"sources": [{"name": "a", "path": "a.geojson"}, {"name": "a", "path": "b.geojson"}]}
        )


def test_empty_sources_rejected() -> None:
    with pytest.raises(ConfigError, match="sources"):
        parse_config({"sources": []})


def test_unknown_key_rejected() -> None:
    with pytest.raises(ConfigError, match="reprojection|Extra inputs"):
        parse_config({"sources": [{"name": "a", "path": "a.geojson"}], "reprojection": "epsg:3857"})


def test_invalid_source_name_rejected() -> None:
    with pytest.raises(ConfigError, match="pattern|String should match"):
        parse_config({"sources": [{"name": "not a name!", "path": "a.geojson"}]})


def test_bad_url_scheme_rejected() -> None:
    with pytest.raises(ConfigError, match="http:// or https://"):
        parse_config({"sources": [{"name": "a", "type": "http", "url": "ftp://example.org/x"}]})


def test_durations_normalise_to_seconds() -> None:
    config = parse_config(
        {
            "defaults": {"timeout": "1m", "max_age": "2h"},
            "sources": [{"name": "a", "path": "a.geojson", "max_age": "90s"}],
        }
    )
    assert config.defaults.timeout == 60
    assert config.effective(config.sources[0], "max_age") == 90
    assert config.defaults.max_age == 7200


def test_effective_falls_back_to_defaults() -> None:
    config = parse_config(
        {"defaults": {"retries": 7}, "sources": [{"name": "a", "path": "a.geojson"}]}
    )
    assert config.effective(config.sources[0], "retries") == 7


def test_relative_paths_resolve_against_the_config_directory(tmp_path: Path) -> None:
    config = parse_config({"sources": [{"name": "a", "path": "a.geojson"}]}, base_dir=tmp_path)
    assert config.resolve("build/out.geojson") == (tmp_path / "build/out.geojson").resolve()
    assert config.resolve("/absolute/out.geojson") == Path("/absolute/out.geojson")


def test_point_mapping_parsed() -> None:
    config = parse_config(
        {
            "sources": [
                {
                    "name": "a",
                    "path": "a.csv",
                    "format": "csv",
                    "mapping": {
                        "geometry": {"type": "point", "lon": "longitude", "lat": "latitude"}
                    },
                }
            ]
        }
    )
    mapping = config.sources[0].mapping
    assert mapping is not None
    assert isinstance(mapping.geometry, PointMapping)
    assert mapping.properties == "*"


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration file not found"):
        load_config(tmp_path / "nope.yml")


def test_load_config_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bad.yml"
    path.write_text("sources: [\n  - name: a\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_load_config_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "list.yml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a YAML mapping"):
        load_config(path)


def test_load_config_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.yml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="is empty"):
        load_config(path)


def test_source_by_name_and_lookup_miss() -> None:
    config = parse_config({"sources": [{"name": "a", "path": "a.geojson"}]})
    assert config.source_by_name("a") is not None
    assert config.source_by_name("b") is None


def test_example_configs_are_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("pipeline.yml", "multi-source.yml"):
        config = load_config(root / "examples" / name)
        assert config.sources
