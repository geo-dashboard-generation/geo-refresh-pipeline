"""SQLAlchemy is optional: this module must pass with it uninstalled.

CI runs this file in an environment without SQLAlchemy to prove the `sql`
source degrades with an actionable message instead of an ImportError.
"""

from __future__ import annotations

import builtins

import pytest

from geo_refresh.config import parse_config
from geo_refresh.errors import MissingDependencyError
from geo_refresh.sources import fetch_sql

CONFIG = parse_config(
    {
        "sources": [
            {
                "name": "cities",
                "type": "sql",
                "url": "sqlite://",
                "query": "SELECT 1 AS lon, 2 AS lat",
                "format": "json",
                "mapping": {"geometry": {"type": "point", "lon": "lon", "lat": "lat"}},
            }
        ]
    }
)


def test_config_with_a_sql_source_parses_without_sqlalchemy() -> None:
    """Parsing must never import the driver, so `validate` works anywhere."""
    assert CONFIG.sources[0].type == "sql"


def test_sql_source_missing_dependency_degrades_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name == "sqlalchemy":
            raise ImportError("No module named 'sqlalchemy'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(MissingDependencyError) as excinfo:
        fetch_sql(CONFIG.sources[0], CONFIG)
    message = str(excinfo.value)
    assert "needs SQLAlchemy" in message
    assert "pip install" in message
