"""The browser badge.

The JS is exercised under Node so its freshness logic is genuinely tested, not
just checked for existence. It is also compared against the Python
implementation, since the two must agree on what "3 hours ago" means.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from geo_refresh.manifest import humanize_age

ASSETS = Path(__file__).resolve().parents[1] / "src" / "geo_refresh" / "assets"
BADGE_JS = ASSETS / "freshness-badge.js"
BADGE_CSS = ASSETS / "freshness-badge.css"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_node(script: str, tmp_path: Path) -> dict:
    """Load the badge in Node and return whatever the script prints as JSON."""
    entry = tmp_path / "run.mjs"
    entry.write_text(
        "import { readFileSync } from 'node:fs';\n"
        "const globalObject = { };\n"
        f"const source = readFileSync({json.dumps(str(BADGE_JS))}, 'utf8');\n"
        "new Function('window', 'globalThis', source)(globalObject, globalObject);\n"
        "const geoRefreshBadge = globalObject.geoRefreshBadge;\n"
        f"{script}\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(NODE), str(entry)], capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def manifest(**sources: dict) -> str:
    return json.dumps({"schema": "geo-refresh-manifest/1", "sources": sources})


def source(fetched_at: str, max_age: int | None = 21600, **extra: object) -> dict:
    return {
        "status": "ok",
        "fetched_at": fetched_at,
        "max_age_seconds": max_age,
        "feature_count": 20,
        **extra,
    }


# -- static checks (always run) --------------------------------------------- #


def test_badge_assets_exist_and_are_self_contained() -> None:
    js = BADGE_JS.read_text(encoding="utf-8")
    assert "geoRefreshBadge" in js
    assert "import " not in js and "require(" not in js, "the badge must be dependency-free"
    assert BADGE_CSS.exists()


def test_badge_css_styles_every_state() -> None:
    css = BADGE_CSS.read_text(encoding="utf-8")
    for state in ("fresh", "stale", "failed", "unknown"):
        assert f'[data-state="{state}"]' in css
    assert "prefers-color-scheme: dark" in css
    assert "prefers-reduced-motion" in css


def test_badge_is_shipped_by_init(tmp_path: Path) -> None:
    from geo_refresh.scaffold import scaffold_files

    files = scaffold_files()
    assert files["freshness-badge.js"] == BADGE_JS.read_text(encoding="utf-8")
    assert "geoRefreshBadge.mount" in files["freshness-demo.html"]


# -- behaviour under Node --------------------------------------------------- #


@requires_node
@pytest.mark.parametrize(
    "seconds", [0, 44, 60, 600, 3600, 10800, 86400, 604800, 2592000, 31536000]
)
def test_humanize_age_matches_the_python_implementation(seconds: int, tmp_path: Path) -> None:
    result = run_node(
        f"console.log(JSON.stringify({{ text: geoRefreshBadge.humanizeAge({seconds}) }}));",
        tmp_path,
    )
    assert result["text"] == humanize_age(seconds)


@requires_node
def test_fresh_manifest_renders_an_updated_label(tmp_path: Path) -> None:
    document = manifest(cities=source("2026-07-19T09:00:00Z"))
    result = run_node(
        f"const m = {document};\n"
        "const now = Date.parse('2026-07-19T12:00:00Z');\n"
        "console.log(JSON.stringify(geoRefreshBadge.evaluate(m, { now })));",
        tmp_path,
    )
    assert result["state"] == "fresh"
    assert result["label"] == "Updated 3 hours ago"


@requires_node
def test_stale_manifest_renders_a_warning(tmp_path: Path) -> None:
    document = manifest(cities=source("2026-07-18T00:00:00Z", 3600))
    result = run_node(
        f"const m = {document};\n"
        "const now = Date.parse('2026-07-19T12:00:00Z');\n"
        "console.log(JSON.stringify(geoRefreshBadge.evaluate(m, { now })));",
        tmp_path,
    )
    assert result["state"] == "stale"
    assert "may be out of date" in result["label"]


@requires_node
def test_failed_source_takes_priority_over_staleness(tmp_path: Path) -> None:
    document = manifest(
        cities=source("2026-07-19T11:59:00Z", 3600, status="failed", error="upstream 503")
    )
    result = run_node(
        f"const m = {document};\n"
        "const now = Date.parse('2026-07-19T12:00:00Z');\n"
        "console.log(JSON.stringify(geoRefreshBadge.evaluate(m, { now })));",
        tmp_path,
    )
    assert result["state"] == "failed"
    assert "upstream 503" in result["title"]


@requires_node
def test_the_oldest_source_drives_the_badge(tmp_path: Path) -> None:
    document = manifest(
        recent=source("2026-07-19T11:55:00Z"), old=source("2026-07-19T06:00:00Z")
    )
    result = run_node(
        f"const m = {document};\n"
        "const now = Date.parse('2026-07-19T12:00:00Z');\n"
        "console.log(JSON.stringify(geoRefreshBadge.evaluate(m, { now })));",
        tmp_path,
    )
    assert result["label"] == "Updated 6 hours ago"


@requires_node
def test_a_single_source_can_be_selected(tmp_path: Path) -> None:
    document = manifest(
        recent=source("2026-07-19T11:55:00Z"), old=source("2026-07-19T06:00:00Z")
    )
    result = run_node(
        f"const m = {document};\n"
        "const now = Date.parse('2026-07-19T12:00:00Z');\n"
        "console.log(JSON.stringify(geoRefreshBadge.evaluate(m, { now, source: 'recent' })));",
        tmp_path,
    )
    assert result["label"] == "Updated 5 minutes ago"
    assert len(result["sources"]) == 1


@requires_node
def test_a_source_without_max_age_never_goes_stale(tmp_path: Path) -> None:
    document = manifest(cities=source("2020-01-01T00:00:00Z", None))
    result = run_node(
        f"const m = {document};\n"
        "const now = Date.parse('2026-07-19T12:00:00Z');\n"
        "console.log(JSON.stringify(geoRefreshBadge.evaluate(m, { now })));",
        tmp_path,
    )
    assert result["state"] == "fresh"


@requires_node
def test_an_empty_manifest_reports_unknown(tmp_path: Path) -> None:
    result = run_node(
        "console.log(JSON.stringify(geoRefreshBadge.evaluate({ sources: {} }, {})));",
        tmp_path,
    )
    assert result["state"] == "unknown"


@requires_node
def test_the_badge_reads_a_manifest_this_pipeline_wrote(tmp_path: Path) -> None:
    """Round trip: run the real pipeline, then feed its manifest to the badge."""
    from geo_refresh.cli import main
    import io

    main(["init", str(tmp_path)], out=io.StringIO())
    main(["run", str(tmp_path / "pipeline.yml"), "--log-level", "silent"], out=io.StringIO())
    document = (tmp_path / "build/manifest.json").read_text(encoding="utf-8")
    result = run_node(
        f"const m = {document};\n"
        "console.log(JSON.stringify(geoRefreshBadge.evaluate(m, {})));",
        tmp_path,
    )
    assert result["state"] == "fresh"
    assert result["label"] == "Updated just now"
    assert result["sources"][0]["featureCount"] == 20
