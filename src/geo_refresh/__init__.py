"""geo-refresh-pipeline: a declarative, scheduled refresh pipeline for map data.

Fetch from HTTP APIs, local files or SQL; transform and validate; detect whether
anything actually changed via an order-stable content hash; regenerate build
artifacts only when it did; and emit a freshness manifest a dashboard can read
to show a stale-data badge.

The public surface is deliberately small::

    from geo_refresh import load_config, run_pipeline, RunOptions

    config = load_config("pipeline.yml")
    result = run_pipeline(config, RunOptions(dry_run=True))
    print(result.changed, result.manifest.feature_count)
"""

from __future__ import annotations

__version__ = "1.0.0"

from .config import PipelineConfig, load_config, parse_config
from .diffing import DiffSummary, compute_diff
from .errors import (
    ConfigError,
    FetchError,
    GeoRefreshError,
    MissingDependencyError,
    OutputError,
    ValidationError,
)
from .hashing import collection_hash, feature_hash
from .manifest import Manifest, SourceManifest, compute_stale, humanize_age
from .pipeline import RunOptions, RunResult, run_pipeline

__all__ = [
    "__version__",
    "ConfigError",
    "DiffSummary",
    "FetchError",
    "GeoRefreshError",
    "Manifest",
    "MissingDependencyError",
    "OutputError",
    "PipelineConfig",
    "RunOptions",
    "RunResult",
    "SourceManifest",
    "ValidationError",
    "collection_hash",
    "compute_diff",
    "compute_stale",
    "feature_hash",
    "humanize_age",
    "load_config",
    "parse_config",
    "run_pipeline",
]
