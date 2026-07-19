"""CLI behaviour: arguments, output, exit codes, scaffolding, logging."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from conftest import geojson, point_feature
from geo_refresh.cli import main
from geo_refresh.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_NO_CHANGE,
    EXIT_OK,
    EXIT_VALIDATION_FAILURE,
)
from geo_refresh.logging import Logger

BERLIN = point_feature("berlin", 13.404954, 52.520008, city="Berlin")
PARIS = point_feature("paris", 2.352222, 48.856613, city="Paris")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tiny working project rooted at ``tmp_path``."""
    (tmp_path / "cities.geojson").write_text(geojson([BERLIN]), encoding="utf-8")
    (tmp_path / "pipeline.yml").write_text(
        yaml.safe_dump(
            {
                "manifest": "build/manifest.json",
                "state": "build/state.json",
                "sources": [
                    {
                        "name": "cities",
                        "path": "cities.geojson",
                        "id_property": "id",
                        "max_age": "6h",
                        "outputs": [{"geojson": "build/cities.geojson"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def run(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(args), out=out)
    return code, out.getvalue()


# -- run -------------------------------------------------------------------- #


def test_run_reports_the_change_then_no_change(project: Path) -> None:
    config = str(project / "pipeline.yml")
    code, output = run("run", config, "--log-level", "silent")
    assert code == EXIT_OK
    assert "changed" in output
    assert "build/cities.geojson" in output

    code, output = run("run", config, "--log-level", "silent")
    assert code == EXIT_NO_CHANGE
    assert "Nothing changed upstream" in output


def test_run_dry_run_writes_nothing(project: Path) -> None:
    code, output = run("run", str(project / "pipeline.yml"), "--dry-run", "--log-level", "silent")
    assert code == EXIT_OK
    assert "would write geojson" in output
    assert not (project / "build").exists()


def test_run_force(project: Path) -> None:
    config = str(project / "pipeline.yml")
    run("run", config, "--log-level", "silent")
    code, _ = run("run", config, "--force", "--log-level", "silent")
    assert code == EXIT_OK


def test_run_only_unknown_source_exits_with_a_validation_code(project: Path) -> None:
    code, _ = run("run", str(project / "pipeline.yml"), "--only", "nope", "--log-level", "silent")
    assert code == EXIT_VALIDATION_FAILURE


def test_run_missing_config_exits_with_a_config_code(tmp_path: Path) -> None:
    code, _ = run("run", str(tmp_path / "absent.yml"))
    assert code == EXIT_CONFIG_ERROR


def test_run_writes_a_github_style_summary(project: Path) -> None:
    summary = project / "summary.txt"
    run(
        "run",
        str(project / "pipeline.yml"),
        "--summary",
        str(summary),
        "--log-level",
        "silent",
    )
    lines = dict(
        line.split("=", 1) for line in summary.read_text(encoding="utf-8").strip().splitlines()
    )
    assert lines["changed"] == "true"
    assert lines["stale"] == "false"
    assert lines["feature-count"] == "1"
    assert lines["exit-code"] == "0"
    assert lines["manifest-path"].endswith("manifest.json")


def test_run_summary_reports_no_change_on_the_second_run(project: Path) -> None:
    summary = project / "summary.txt"
    config = str(project / "pipeline.yml")
    run("run", config, "--log-level", "silent")
    run("run", config, "--summary", str(summary), "--log-level", "silent")
    text = summary.read_text(encoding="utf-8")
    assert "changed=false" in text
    assert "exit-code=3" in text


def test_run_failure_is_reported_on_stdout(project: Path) -> None:
    (project / "cities.geojson").unlink()
    code, output = run("run", str(project / "pipeline.yml"), "--log-level", "silent")
    assert code == 4
    assert "FAILED" in output
    assert "Run failed: fetch-failure" in output


# -- validate --------------------------------------------------------------- #


def test_validate_describes_the_plan(project: Path) -> None:
    code, output = run("validate", str(project / "pipeline.yml"))
    assert code == EXIT_OK
    assert "is valid." in output
    assert "cities (file, geojson)" in output
    assert "diff key: properties.id" in output


def test_validate_catches_a_bad_filter_expression(tmp_path: Path) -> None:
    (tmp_path / "p.yml").write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "name": "a",
                        "path": "a.geojson",
                        "transform": [{"filter": "open(  'x'  )"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    code, _ = run("validate", str(tmp_path / "p.yml"))
    assert code == EXIT_CONFIG_ERROR


def test_validate_makes_no_network_or_disk_access(tmp_path: Path) -> None:
    (tmp_path / "p.yml").write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {"name": "a", "type": "http", "url": "https://nonexistent.invalid/a.geojson"}
                ]
            }
        ),
        encoding="utf-8",
    )
    code, _ = run("validate", str(tmp_path / "p.yml"))
    assert code == EXIT_OK


# -- status ----------------------------------------------------------------- #


def test_status_pretty_prints_the_manifest(project: Path) -> None:
    run("run", str(project / "pipeline.yml"), "--log-level", "silent")
    code, output = run("status", str(project / "build/manifest.json"))
    assert code == EXIT_OK
    assert "cities" in output
    assert "just now" in output
    assert "1 sources" in output


def test_status_accepts_a_config_path(project: Path) -> None:
    run("run", str(project / "pipeline.yml"), "--log-level", "silent")
    code, output = run("status", str(project / "pipeline.yml"))
    assert code == EXIT_OK
    assert "manifest:" in output


def test_status_json_output_is_parseable(project: Path) -> None:
    run("run", str(project / "pipeline.yml"), "--log-level", "silent")
    code, output = run("status", str(project / "build/manifest.json"), "--json")
    assert code == EXIT_OK
    assert json.loads(output)["schema"] == "geo-refresh-manifest/1"


def test_status_flags_stale_data_with_a_non_zero_exit(project: Path) -> None:
    run("run", str(project / "pipeline.yml"), "--log-level", "silent")
    path = project / "build/manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["sources"]["cities"]["stale"] = True
    document["sources"]["cities"]["fetched_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(document), encoding="utf-8")
    code, output = run("status", str(path))
    assert code == EXIT_VALIDATION_FAILURE
    assert "STALE" in output
    assert "past its max_age" in output


def test_status_on_a_missing_manifest(tmp_path: Path) -> None:
    code, _ = run("status", str(tmp_path / "manifest.json"))
    assert code == EXIT_VALIDATION_FAILURE


def test_status_shows_the_diff_and_the_error(project: Path) -> None:
    config = str(project / "pipeline.yml")
    run("run", config, "--log-level", "silent")
    (project / "cities.geojson").write_text(geojson([BERLIN, PARIS]), encoding="utf-8")
    run("run", config, "--log-level", "silent")
    _, output = run("status", str(project / "build/manifest.json"))
    assert "+1 / -0 / ~0" in output


# -- init ------------------------------------------------------------------- #


def test_init_creates_a_runnable_project(tmp_path: Path) -> None:
    target = tmp_path / "project"
    code, output = run("init", str(target))
    assert code == EXIT_OK
    assert (target / "pipeline.yml").exists()
    assert (target / "data/stations.csv").exists()
    assert (target / "freshness-badge.js").exists()
    assert (target / "freshness-badge.css").exists()
    assert (target / "freshness-demo.html").exists()
    assert "created" in output

    code, _ = run("run", str(target / "pipeline.yml"), "--log-level", "silent")
    assert code == EXIT_OK
    manifest = json.loads((target / "build/manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["air_quality_stations"]["feature_count"] == 20


def test_init_does_not_overwrite_by_default(tmp_path: Path) -> None:
    run("init", str(tmp_path))
    (tmp_path / "pipeline.yml").write_text("# edited\n", encoding="utf-8")
    code, output = run("init", str(tmp_path))
    assert code == EXIT_OK
    assert "nothing written" in output
    assert (tmp_path / "pipeline.yml").read_text(encoding="utf-8") == "# edited\n"


def test_init_force_overwrites(tmp_path: Path) -> None:
    run("init", str(tmp_path))
    (tmp_path / "pipeline.yml").write_text("# edited\n", encoding="utf-8")
    run("init", str(tmp_path), "--force")
    assert "sources:" in (tmp_path / "pipeline.yml").read_text(encoding="utf-8")


def test_scaffolded_pipeline_filters_the_decommissioned_station(tmp_path: Path) -> None:
    run("init", str(tmp_path))
    run("run", str(tmp_path / "pipeline.yml"), "--log-level", "silent")
    features = json.loads(
        (tmp_path / "build/air_quality_stations.geojson").read_text(encoding="utf-8")
    )["features"]
    codes = {f["properties"]["station_id"] for f in features}
    assert "RO-BU-0025" not in codes
    assert "pm25_ug_m3" in features[0]["properties"]


# -- parser and logging ----------------------------------------------------- #


def test_help_lists_every_command() -> None:
    from geo_refresh.cli import build_parser

    text = build_parser().format_help()
    for command in ("run", "validate", "status", "init"):
        assert command in text
    assert "exit codes" in text


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_json_logging_emits_one_object_per_line(project: Path) -> None:
    import contextlib

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        main(["run", str(project / "pipeline.yml"), "--log-format", "json"], out=io.StringIO())
    lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
    assert lines
    for line in lines:
        payload = json.loads(line)
        assert {"ts", "level", "event"} <= set(payload)


def test_logger_respects_the_level() -> None:
    stream = io.StringIO()
    logger = Logger(fmt="text", level="warning", stream=stream, colour=False)
    logger.debug("hidden")
    logger.info("also.hidden")
    logger.warning("shown", count=2)
    logger.error("bad", reason="two words")
    assert "hidden" not in stream.getvalue()
    assert "shown count=2" in stream.getvalue()
    assert 'reason="two words"' in stream.getvalue()


def test_logger_rejects_an_unknown_level() -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        Logger(level="chatty")
