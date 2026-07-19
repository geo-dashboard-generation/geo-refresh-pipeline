"""Shared fixtures. No test in this suite touches the network or the clock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx
import pytest
import yaml

from geo_refresh.config import PipelineConfig, parse_config
from geo_refresh.pipeline import RunOptions


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[dict[str, Any]], PipelineConfig]:
    """Return a helper that materialises a config dict into ``tmp_path``."""

    def _write(data: dict[str, Any], name: str = "pipeline.yml") -> PipelineConfig:
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return parse_config(data, base_dir=tmp_path, origin=str(path))

    return _write


@pytest.fixture
def config_factory(tmp_path: Path) -> Callable[..., PipelineConfig]:
    """Build a config object rooted at ``tmp_path`` without writing YAML."""

    def _build(**overrides: Any) -> PipelineConfig:
        data: dict[str, Any] = {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "sources": [],
        }
        data.update(overrides)
        return parse_config(data, base_dir=tmp_path)

    return _build


def make_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.Client]:
    """Wrap a request handler into the client factory the fetcher expects."""

    def factory(*, timeout: float, verify: bool) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    return factory


def sequence_handler(
    responses: Sequence[httpx.Response | Exception],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    """A handler that returns/raises each item in turn, recording the requests."""
    calls: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = remaining.pop(0) if remaining else responses[-1]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(
            status_code=item.status_code,
            headers=item.headers,
            content=item.content,
            request=request,
        )

    return handler, calls


def json_response(payload: Any, **kwargs: Any) -> httpx.Response:
    """A 200 JSON response."""
    return httpx.Response(200, json=payload, **kwargs)


def geojson(features: list[dict[str, Any]]) -> str:
    """Serialise a FeatureCollection."""
    return json.dumps({"type": "FeatureCollection", "features": features})


def point_feature(
    identifier: str, lon: float, lat: float, **properties: Any
) -> dict[str, Any]:
    """A Point feature with an ``id`` property, for diff tests."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"id": identifier, **properties},
    }


@pytest.fixture
def fast_options() -> Callable[..., RunOptions]:
    """RunOptions with fsync and real sleeping disabled."""

    def _build(**overrides: Any) -> RunOptions:
        defaults: dict[str, Any] = {"fsync": False, "sleep": lambda _seconds: None}
        defaults.update(overrides)
        return RunOptions(**defaults)

    return _build
