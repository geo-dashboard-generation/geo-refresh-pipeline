"""File and SQL sources. SQL runs against an on-disk SQLite database."""

from __future__ import annotations

from pathlib import Path

import pytest

from geo_refresh.config import parse_config
from geo_refresh.errors import FetchError
from geo_refresh.sources import _redact_url, fetch_file, fetch_sql

sqlalchemy = pytest.importorskip("sqlalchemy")

CITIES = [
    ("VIE", "Vienna", 16.373819, 48.208176, 1_920_000),
    ("ZRH", "Zurich", 8.541694, 47.376887, 434_000),
    ("CPH", "Copenhagen", 12.568337, 55.676098, 660_000),
]


@pytest.fixture
def database(tmp_path: Path) -> str:
    """A SQLite database with a small, real city table."""
    path = tmp_path / "cities.sqlite"
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "CREATE TABLE cities (code TEXT, name TEXT, lon REAL, lat REAL, population INT)"
            )
        )
        for row in CITIES:
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO cities VALUES (:code, :name, :lon, :lat, :population)"
                ),
                {
                    "code": row[0],
                    "name": row[1],
                    "lon": row[2],
                    "lat": row[3],
                    "population": row[4],
                },
            )
    engine.dispose()
    return f"sqlite:///{path}"


def sql_config(url: str, query: str = "SELECT * FROM cities ORDER BY code"):
    return parse_config(
        {
            "sources": [
                {
                    "name": "cities",
                    "type": "sql",
                    "url": url,
                    "query": query,
                    "format": "json",
                    "mapping": {
                        "geometry": {"type": "point", "lon": "lon", "lat": "lat"},
                        "properties": ["code", "name", "population"],
                    },
                }
            ]
        }
    )


# -- file source ------------------------------------------------------------ #


def test_file_source_reads_bytes_and_mtime(tmp_path: Path) -> None:
    path = tmp_path / "feed.geojson"
    path.write_text('{"type": "FeatureCollection", "features": []}', encoding="utf-8")
    config = parse_config({"sources": [{"name": "f", "path": "feed.geojson"}]}, base_dir=tmp_path)
    result = fetch_file(config.sources[0], config)
    assert result.payload.startswith(b'{"type"')
    assert result.last_modified is not None and result.last_modified.endswith("GMT")


def test_file_source_missing_file_names_the_base_dir(tmp_path: Path) -> None:
    config = parse_config({"sources": [{"name": "f", "path": "absent.geojson"}]}, base_dir=tmp_path)
    with pytest.raises(FetchError, match="file not found"):
        fetch_file(config.sources[0], config)


def test_file_source_rejects_a_directory(tmp_path: Path) -> None:
    (tmp_path / "adir").mkdir()
    config = parse_config({"sources": [{"name": "f", "path": "adir"}]}, base_dir=tmp_path)
    with pytest.raises(FetchError, match="is a directory"):
        fetch_file(config.sources[0], config)


def test_file_source_expands_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.geojson").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("FEED_DIR", "data")
    config = parse_config(
        {"sources": [{"name": "f", "path": "${FEED_DIR}/x.geojson"}]}, base_dir=tmp_path
    )
    assert fetch_file(config.sources[0], config).payload == b"{}"


# -- sql source ------------------------------------------------------------- #


def test_sql_source_returns_records(database: str) -> None:
    config = sql_config(database)
    result = fetch_sql(config.sources[0], config)
    assert result.records is not None
    assert len(result.records) == 3
    assert result.records[0]["code"] == "CPH"


def test_sql_records_become_features(database: str) -> None:
    from geo_refresh.formats import records_to_features

    config = sql_config(database)
    result = fetch_sql(config.sources[0], config)
    mapping = config.sources[0].mapping
    assert mapping is not None
    features = records_to_features(result.records or [], mapping)
    assert features[0]["geometry"]["coordinates"] == [12.568337, 55.676098]
    assert features[0]["properties"]["name"] == "Copenhagen"


def test_sql_query_error_is_reported_with_the_query(database: str) -> None:
    config = sql_config(database, "SELECT * FROM no_such_table")
    with pytest.raises(FetchError, match="query failed"):
        fetch_sql(config.sources[0], config)


def test_sql_bad_url_is_reported() -> None:
    config = sql_config("not-a-database-url")
    with pytest.raises(FetchError, match="cannot create a database engine"):
        fetch_sql(config.sources[0], config)


def test_sql_credentials_are_redacted_from_the_origin() -> None:
    assert _redact_url("postgresql://user:pw@db.example.org/x") == (
        "postgresql://***@db.example.org/x"
    )
    assert _redact_url("sqlite:///local.db") == "sqlite:///local.db"
