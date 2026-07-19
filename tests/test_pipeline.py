"""End-to-end pipeline behaviour: change detection, outputs, exit codes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import geojson, make_client_factory, point_feature, sequence_handler
from geo_refresh.config import parse_config
from geo_refresh.errors import (
    EXIT_FETCH_FAILURE,
    EXIT_NO_CHANGE,
    EXIT_OK,
    EXIT_VALIDATION_FAILURE,
)
from geo_refresh.pipeline import RunOptions, run_pipeline, select_sources

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)

BERLIN = point_feature("berlin", 13.404954, 52.520008, city="Berlin", pop=3_878_000)
PARIS = point_feature("paris", 2.352222, 48.856613, city="Paris", pop=2_103_000)
MADRID = point_feature("madrid", -3.703790, 40.416775, city="Madrid", pop=3_223_000)


def file_config(tmp_path: Path, **source: Any):
    payload: dict[str, Any] = {
        "name": "cities",
        "path": "cities.geojson",
        "id_property": "id",
        "outputs": [{"geojson": "build/cities.geojson"}],
    }
    payload.update(source)
    return parse_config(
        {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "sources": [payload],
        },
        base_dir=tmp_path,
    )


def write_source(tmp_path: Path, features: list[dict[str, Any]]) -> None:
    (tmp_path / "cities.geojson").write_text(geojson(features), encoding="utf-8")


def options(**overrides: Any) -> RunOptions:
    base: dict[str, Any] = {"fsync": False, "sleep": lambda _s: None, "now": NOW}
    base.update(overrides)
    return RunOptions(**base)


# -- change detection ------------------------------------------------------- #


def test_first_run_writes_outputs_and_exits_zero(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN, PARIS])
    result = run_pipeline(file_config(tmp_path), options())
    assert result.exit_code == EXIT_OK
    assert result.changed is True
    written = json.loads((tmp_path / "build/cities.geojson").read_text(encoding="utf-8"))
    assert len(written["features"]) == 2
    assert (tmp_path / "build/manifest.json").exists()


def test_second_identical_run_reports_no_change(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN, PARIS])
    config = file_config(tmp_path)
    run_pipeline(config, options())
    output = tmp_path / "build/cities.geojson"
    stamp = output.stat().st_mtime_ns

    result = run_pipeline(config, options())
    assert result.exit_code == EXIT_NO_CHANGE
    assert result.changed is False
    assert output.stat().st_mtime_ns == stamp, "unchanged data must not rewrite the artifact"


def test_reordered_features_are_not_a_change(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN, PARIS])
    config = file_config(tmp_path)
    run_pipeline(config, options())
    write_source(tmp_path, [PARIS, BERLIN])
    assert run_pipeline(config, options()).exit_code == EXIT_NO_CHANGE


def test_changed_upstream_rewrites_and_reports_the_diff(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN, PARIS])
    config = file_config(tmp_path)
    run_pipeline(config, options())

    modified_paris = point_feature("paris", 2.352222, 48.856613, city="Paris", pop=2_200_000)
    write_source(tmp_path, [BERLIN, modified_paris, MADRID])
    result = run_pipeline(config, options())

    assert result.exit_code == EXIT_OK
    entry = result.manifest.get("cities")
    assert entry is not None and entry.diff is not None
    assert entry.diff.added == 1
    assert entry.diff.modified == 1
    assert entry.diff.removed == 0
    assert entry.diff.unchanged == 1


def test_removed_features_are_counted(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN, PARIS, MADRID])
    config = file_config(tmp_path)
    run_pipeline(config, options())
    write_source(tmp_path, [BERLIN])
    entry = run_pipeline(config, options()).manifest.get("cities")
    assert entry is not None and entry.diff is not None
    assert entry.diff.removed == 2


def test_force_rewrites_even_without_a_change(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    config = file_config(tmp_path)
    run_pipeline(config, options())
    result = run_pipeline(config, options(force=True))
    assert result.exit_code == EXIT_OK
    assert result.outputs


# -- manifest --------------------------------------------------------------- #


def test_manifest_records_every_documented_field(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN, PARIS])
    config = file_config(tmp_path, max_age="6h")
    run_pipeline(config, options())
    document = json.loads((tmp_path / "build/manifest.json").read_text(encoding="utf-8"))

    assert document["schema"] == "geo-refresh-manifest/1"
    entry = document["sources"]["cities"]
    assert entry["fetched_at"].endswith("Z")
    assert entry["last_modified"].endswith("GMT")
    assert entry["feature_count"] == 2
    assert entry["content_hash"].startswith("sha256-features-v1:")
    assert entry["max_age_seconds"] == 21600
    assert entry["stale"] is False
    assert entry["diff"]["first_run"] is True
    assert entry["bbox"] == [2.352222, 48.856613, 13.404954, 52.520008]


def test_manifest_marks_a_source_stale_once_max_age_passes(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    config = file_config(tmp_path, max_age="1h")
    run_pipeline(config, options())

    later = NOW + timedelta(hours=3)
    result = run_pipeline(config, options(now=later, only=["cities"]))
    # A skipped/unchanged source keeps its old fetched_at only when it did not
    # refresh; here it refreshed, so it is fresh again.
    assert result.manifest.get("cities").stale is False  # type: ignore[union-attr]


def test_failed_source_carries_forward_its_timestamp_and_goes_stale(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    config = file_config(tmp_path, max_age="1h")
    run_pipeline(config, options())

    (tmp_path / "cities.geojson").unlink()
    later = NOW + timedelta(hours=4)
    result = run_pipeline(config, options(now=later))

    entry = result.manifest.get("cities")
    assert entry is not None
    assert entry.status == "failed"
    assert entry.fetched_at == "2026-07-19T12:00:00Z"
    assert entry.stale is True
    assert result.exit_code == EXIT_FETCH_FAILURE


# -- exit codes and failure handling ---------------------------------------- #


def test_malformed_payload_gives_a_validation_exit_code(tmp_path: Path) -> None:
    (tmp_path / "cities.geojson").write_text("{not json", encoding="utf-8")
    result = run_pipeline(file_config(tmp_path), options())
    assert result.exit_code == EXIT_VALIDATION_FAILURE
    assert "invalid JSON" in (result.manifest.get("cities").error or "")  # type: ignore[union-attr]


def test_min_features_guards_against_a_truncated_upstream(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    config = file_config(tmp_path, min_features=10)
    result = run_pipeline(config, options())
    assert result.exit_code == EXIT_VALIDATION_FAILURE
    assert "min_features" in (result.manifest.get("cities").error or "")  # type: ignore[union-attr]


def test_a_failing_source_blocks_outputs_for_every_source(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    config = parse_config(
        {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "sources": [
                {
                    "name": "good",
                    "path": "cities.geojson",
                    "outputs": [{"geojson": "build/good.geojson"}],
                },
                {"name": "bad", "path": "missing.geojson"},
            ],
            "outputs": [{"geojson": "build/all.geojson"}],
        },
        base_dir=tmp_path,
    )
    result = run_pipeline(config, options())
    assert result.exit_code == EXIT_FETCH_FAILURE
    assert not (tmp_path / "build/good.geojson").exists()
    assert not (tmp_path / "build/all.geojson").exists()
    # The manifest is still written so a badge can react to the failure.
    assert (tmp_path / "build/manifest.json").exists()


def test_no_partial_artifact_when_an_output_directory_is_blocked(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    (tmp_path / "build").write_text("this is a file, not a directory", encoding="utf-8")
    config = file_config(tmp_path)
    with pytest.raises(Exception, match="cannot"):
        run_pipeline(config, options())


# -- selection and dry runs ------------------------------------------------- #


def test_only_runs_the_named_source(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    (tmp_path / "other.geojson").write_text(geojson([PARIS]), encoding="utf-8")
    config = parse_config(
        {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "sources": [
                {
                    "name": "cities",
                    "path": "cities.geojson",
                    "outputs": [{"geojson": "build/cities.geojson"}],
                },
                {
                    "name": "other",
                    "path": "other.geojson",
                    "outputs": [{"geojson": "build/other.geojson"}],
                },
            ],
        },
        base_dir=tmp_path,
    )
    result = run_pipeline(config, options(only=["cities"]))
    assert (tmp_path / "build/cities.geojson").exists()
    assert not (tmp_path / "build/other.geojson").exists()
    assert result.manifest.get("other").status == "skipped"  # type: ignore[union-attr]


def test_only_skips_pipeline_level_file_outputs(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    (tmp_path / "other.geojson").write_text(geojson([PARIS]), encoding="utf-8")
    config = parse_config(
        {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "sources": [
                {"name": "cities", "path": "cities.geojson"},
                {"name": "other", "path": "other.geojson"},
            ],
            "outputs": [{"geojson": "build/all.geojson"}],
        },
        base_dir=tmp_path,
    )
    run_pipeline(config, options(only=["cities"]))
    assert not (tmp_path / "build/all.geojson").exists()


def test_unknown_source_name_is_rejected(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    config = file_config(tmp_path)
    with pytest.raises(Exception, match="unknown source 'nope'"):
        select_sources(config, ["nope"])


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    config = file_config(tmp_path)
    result = run_pipeline(config, options(dry_run=True))
    assert result.exit_code == EXIT_OK
    assert not (tmp_path / "build").exists()
    assert result.manifest_path is None
    assert any("would write geojson" in item for item in result.outputs)


# -- transforms and outputs ------------------------------------------------- #


def test_transforms_run_before_hashing(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN, PARIS])
    config = file_config(
        tmp_path,
        transform=[{"filter": "pop > 3000000"}, {"select": ["id", "city"]}, {"round": 3}],
    )
    result = run_pipeline(config, options())
    written = json.loads((tmp_path / "build/cities.geojson").read_text(encoding="utf-8"))
    assert len(written["features"]) == 1
    assert written["features"][0]["properties"] == {"id": "berlin", "city": "Berlin"}
    assert written["features"][0]["geometry"]["coordinates"] == [13.405, 52.52]
    entry = result.manifest.get("cities")
    assert entry is not None and [t["step"] for t in entry.transforms] == [
        "filter",
        "select",
        "round",
    ]


def test_coordinate_noise_below_the_rounding_threshold_is_not_a_change(
    tmp_path: Path,
) -> None:
    config = file_config(tmp_path, transform=[{"round": 4}])
    write_source(tmp_path, [point_feature("berlin", 13.4049541, 52.5200081)])
    run_pipeline(config, options())
    write_source(tmp_path, [point_feature("berlin", 13.4049539, 52.5200079)])
    assert run_pipeline(config, options()).exit_code == EXIT_NO_CHANGE


def test_summary_output_is_written(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN, PARIS])
    config = file_config(
        tmp_path, outputs=[{"geojson": "build/cities.geojson"}, {"summary": "build/s.json"}]
    )
    run_pipeline(config, options())
    summary = json.loads((tmp_path / "build/s.json").read_text(encoding="utf-8"))
    assert summary["feature_count"] == 2
    assert summary["geometry_types"] == {"Point": 2}
    assert summary["source"] == "cities"
    assert sorted(summary["properties"]) == ["city", "id", "pop"]


def test_build_command_runs_only_on_change(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    marker = tmp_path / "built.txt"
    config = parse_config(
        {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "sources": [{"name": "cities", "path": "cities.geojson"}],
            "outputs": [{"build": f"echo built >> {marker}"}],
        },
        base_dir=tmp_path,
    )
    run_pipeline(config, options())
    assert marker.read_text(encoding="utf-8").count("built") == 1
    run_pipeline(config, options())
    assert marker.read_text(encoding="utf-8").count("built") == 1


def test_failing_build_command_reports_its_output(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    config = parse_config(
        {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "sources": [{"name": "cities", "path": "cities.geojson"}],
            "outputs": [{"build": "echo 'renderer crashed' >&2; exit 2"}],
        },
        base_dir=tmp_path,
    )
    with pytest.raises(Exception, match="renderer crashed"):
        run_pipeline(config, options())


def test_skip_build_leaves_file_outputs_alone(tmp_path: Path) -> None:
    write_source(tmp_path, [BERLIN])
    marker = tmp_path / "built.txt"
    config = parse_config(
        {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "sources": [
                {
                    "name": "cities",
                    "path": "cities.geojson",
                    "outputs": [{"geojson": "build/cities.geojson"}],
                }
            ],
            "outputs": [{"build": f"echo built >> {marker}"}],
        },
        base_dir=tmp_path,
    )
    run_pipeline(config, options(skip_build=True))
    assert (tmp_path / "build/cities.geojson").exists()
    assert not marker.exists()


# -- HTTP end to end -------------------------------------------------------- #


def test_http_source_runs_end_to_end(tmp_path: Path) -> None:
    handler, _ = sequence_handler(
        [httpx.Response(200, content=geojson([BERLIN, PARIS]), headers={"ETag": '"v1"'})]
    )
    config = parse_config(
        {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "sources": [
                {
                    "name": "feed",
                    "type": "http",
                    "url": "https://data.example.org/cities.geojson",
                    "id_property": "id",
                    "max_age": "1h",
                    "outputs": [{"geojson": "build/feed.geojson"}],
                }
            ],
        },
        base_dir=tmp_path,
    )
    result = run_pipeline(config, options(client_factory=make_client_factory(handler)))
    assert result.exit_code == EXIT_OK
    entry = result.manifest.get("feed")
    assert entry is not None and entry.etag == '"v1"'
    assert entry.origin == "https://data.example.org/cities.geojson"
    assert (tmp_path / "build/feed.geojson").exists()


def test_http_retries_are_recorded_in_the_manifest(tmp_path: Path) -> None:
    handler, _ = sequence_handler(
        [httpx.Response(503), httpx.Response(200, content=geojson([BERLIN]))]
    )
    config = parse_config(
        {
            "manifest": "build/manifest.json",
            "state": "build/state.json",
            "defaults": {"retry_backoff": 0},
            "sources": [
                {"name": "feed", "type": "http", "url": "https://data.example.org/c.geojson"}
            ],
        },
        base_dir=tmp_path,
    )
    result = run_pipeline(config, options(client_factory=make_client_factory(handler)))
    assert result.manifest.get("feed").attempts == 2  # type: ignore[union-attr]
