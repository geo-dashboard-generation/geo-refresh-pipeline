"""Writing artifacts and running the build command.

All file writes go through :mod:`geo_refresh.atomic`. Build commands run last,
after every artifact is on disk, so the build always sees a complete data set.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from .atomic import atomic_write_json
from .config import BuildOutput, GeoJsonOutput, Output, PipelineConfig, SummaryOutput
from .errors import OutputError
from .geometry import bounding_box
from .hashing import collection_hash
from .logging import Logger

Feature = dict[str, Any]


def feature_collection(features: Sequence[Feature]) -> dict[str, Any]:
    """Wrap features in a GeoJSON ``FeatureCollection``."""
    return {"type": "FeatureCollection", "features": list(features)}


def build_summary(
    features: Sequence[Feature], *, name: str | None = None
) -> dict[str, Any]:
    """Compute a compact summary of a feature collection.

    Includes the feature count, bounding box, geometry-type histogram, the
    property names present, and the content hash — enough for a dashboard to
    show "what's in this file" without loading the file.
    """
    geometry_types = Counter(
        (feature.get("geometry") or {}).get("type", "null")
        if isinstance(feature.get("geometry"), dict)
        else "null"
        for feature in features
    )
    property_names: set[str] = set()
    for feature in features:
        property_names.update((feature.get("properties") or {}).keys())
    summary: dict[str, Any] = {
        "feature_count": len(features),
        "bbox": bounding_box(features),
        "geometry_types": dict(sorted(geometry_types.items())),
        "properties": sorted(property_names),
        "content_hash": collection_hash(features),
    }
    if name is not None:
        summary["source"] = name
    return summary


def write_geojson(
    output: GeoJsonOutput,
    features: Sequence[Feature],
    config: PipelineConfig,
    *,
    fsync: bool = True,
) -> Path:
    """Write a ``FeatureCollection`` atomically."""
    path = config.resolve(output.path)
    return atomic_write_json(
        path, feature_collection(features), indent=output.indent, fsync=fsync
    )


def write_summary(
    output: SummaryOutput,
    features: Sequence[Feature],
    config: PipelineConfig,
    *,
    name: str | None = None,
    fsync: bool = True,
) -> Path:
    """Write a JSON summary atomically."""
    path = config.resolve(output.path)
    return atomic_write_json(path, build_summary(features, name=name), fsync=fsync)


def run_build(
    output: BuildOutput,
    config: PipelineConfig,
    *,
    logger: Logger | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run a build command.

    The command runs through the shell so pipelines and ``&&`` work as written
    in the config. Its stdout and stderr are captured and, on failure, quoted
    back in the error, because a build failing inside a nightly cron job with
    no output is the single most annoying way for this to break.

    Returns:
        The command's stdout.

    Raises:
        OutputError: On a non-zero exit or a timeout.
    """
    cwd = config.resolve(output.cwd) if output.cwd else config.base_dir
    env = {**os.environ, **output.env, **(extra_env or {})}
    if logger:
        logger.info("build.run", command=output.command, cwd=str(cwd))
    try:
        completed = subprocess.run(  # noqa: S602 - shell is the documented contract
            output.command,
            shell=True,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=output.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OutputError(
            f"build command timed out after {output.timeout:g}s: {output.command}"
        ) from exc
    except OSError as exc:
        raise OutputError(f"cannot run build command {output.command!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        tail = "\n".join(detail.splitlines()[-20:])
        raise OutputError(
            f"build command exited {completed.returncode}: {output.command}\n{tail}"
        )
    return completed.stdout


def apply_outputs(
    outputs: Sequence[Output],
    features: Sequence[Feature],
    config: PipelineConfig,
    *,
    name: str | None = None,
    logger: Logger | None = None,
    dry_run: bool = False,
    fsync: bool = True,
) -> list[str]:
    """Run every output in order, returning a description of each.

    In ``dry_run`` mode nothing is written or executed; the descriptions are
    prefixed with ``would `` so the CLI output reads naturally.

    Raises:
        OutputError: If any output fails.
    """
    performed: list[str] = []
    for output in outputs:
        if isinstance(output, GeoJsonOutput):
            path = config.resolve(output.path)
            if dry_run:
                performed.append(f"would write geojson {path} ({len(features)} features)")
                continue
            write_geojson(output, features, config, fsync=fsync)
            performed.append(str(path))
            if logger:
                logger.info(
                    "output.geojson", path=str(path), features=len(features), source=name
                )
        elif isinstance(output, SummaryOutput):
            path = config.resolve(output.path)
            if dry_run:
                performed.append(f"would write summary {path}")
                continue
            write_summary(output, features, config, name=name, fsync=fsync)
            performed.append(str(path))
            if logger:
                logger.info("output.summary", path=str(path), source=name)
        elif isinstance(output, BuildOutput):
            if dry_run:
                performed.append(f"would run build: {output.command}")
                continue
            run_build(output, config, logger=logger)
            performed.append(f"build: {output.command}")
        else:  # pragma: no cover - discriminated union is exhaustive
            raise OutputError(f"unknown output type: {output!r}")
    return performed
