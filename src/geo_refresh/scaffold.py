"""The ``init`` command: write a small, immediately runnable example.

Everything written here is real: the coordinates are actual city centres, the
config runs end to end with no network access, and the badge assets are the
same files the README documents.
"""

from __future__ import annotations

from pathlib import Path

from .atomic import atomic_write_text

ASSETS_DIR = Path(__file__).parent / "assets"

STARTER_CONFIG = """\
# geo-refresh-pipeline example.
#
#   python -m geo_refresh validate pipeline.yml
#   python -m geo_refresh run pipeline.yml
#
# Run it twice: the second run exits 3 (no-change) and rewrites nothing.
version: 1

manifest: build/manifest.json
state: build/.geo-refresh-state.json

defaults:
  timeout: 30s
  retries: 3
  retry_backoff: 500
  max_age: 24h

sources:
  # A local CSV, mapped into Point features by column name.
  - name: air_quality_stations
    path: data/stations.csv
    format: csv
    id_property: station_id
    max_age: 6h
    min_features: 5
    mapping:
      geometry:
        type: point
        lon: longitude
        lat: latitude
      properties: "*"
    transform:
      - filter: "status == 'active' and pm25 >= 0"
      - select: [station_id, city, country, pm25, status]
      - rename: {pm25: pm25_ug_m3}
      - round: 5
      - drop_invalid: true
    outputs:
      - geojson: build/air_quality_stations.geojson
      - summary: build/air_quality_stations.summary.json

  # An HTTP source is configured the same way. Uncomment and point it at a real
  # feed; ${TOKEN} is read from the environment at fetch time, never stored here.
  #
  # - name: live_incidents
  #   url: https://example.org/api/incidents
  #   format: json
  #   id_property: incident_id
  #   max_age: 15m
  #   headers:
  #     Authorization: "Bearer ${INCIDENTS_TOKEN}"
  #   mapping:
  #     records: "$.data.incidents"
  #     geometry:
  #       type: point
  #       lon: lon
  #       lat: lat
  #     properties: [incident_id, kind, reported_at]
  #   outputs:
  #     - geojson: build/live_incidents.geojson

outputs:
  - geojson: build/all_features.geojson
  # - build: "python scripts/render_map.py"
"""

SAMPLE_CSV = """\
station_id,city,country,latitude,longitude,pm25,status
DE-BE-0012,Berlin,DE,52.520008,13.404954,11.4,active
DE-HH-0031,Hamburg,DE,53.551086,9.993682,9.8,active
DE-MU-0007,Munich,DE,48.135125,11.581981,13.1,active
AT-VI-0003,Vienna,AT,48.208176,16.373819,14.6,active
CH-ZH-0021,Zurich,CH,47.376887,8.541694,8.2,active
FR-PA-0044,Paris,FR,48.856613,2.352222,16.9,active
NL-AM-0009,Amsterdam,NL,52.367573,4.904138,10.3,active
BE-BR-0015,Brussels,BE,50.850346,4.351721,12.7,active
CZ-PR-0002,Prague,CZ,50.075539,14.437800,15.2,active
PL-WA-0018,Warsaw,PL,52.229675,21.012230,18.4,active
IT-MI-0027,Milan,IT,45.464203,9.189982,21.8,active
ES-MA-0005,Madrid,ES,40.416775,-3.703790,13.9,active
PT-LI-0011,Lisbon,PT,38.722252,-9.139337,10.1,active
SE-ST-0004,Stockholm,SE,59.329323,18.068581,6.4,active
DK-CO-0013,Copenhagen,DK,55.676098,12.568337,9.1,active
NO-OS-0006,Oslo,NO,59.913868,10.752245,7.3,active
FI-HE-0008,Helsinki,FI,60.169857,24.938379,6.9,active
IE-DU-0010,Dublin,IE,53.349805,-6.260310,8.7,active
HU-BU-0019,Budapest,HU,47.497913,19.040236,17.5,active
GR-AT-0022,Athens,GR,37.983810,23.727539,19.3,active
RO-BU-0025,Bucharest,RO,44.426765,26.102538,20.6,decommissioned
"""

DEMO_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stale-data badge</title>
  <link rel="stylesheet" href="freshness-badge.css">
  <style>
    body {
      margin: 0;
      padding: 3rem 1.5rem;
      font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
      color: #1c1f24;
      background: #fff;
    }
    main { max-width: 44rem; margin: 0 auto; }
    h1 { font-size: 1.5rem; margin: 0 0 0.5rem; }
    p { color: #4a5058; }
    .map-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
      padding-bottom: 0.75rem;
      border-bottom: 1px solid #e3e6ea;
      margin-bottom: 1.5rem;
    }
    @media (prefers-color-scheme: dark) {
      body { color: #e6e9ee; background: #14161a; }
      p { color: #a7aeb8; }
      .map-header { border-color: #2a2e35; }
    }
  </style>
</head>
<body>
  <main>
    <div class="map-header">
      <h1>Air quality stations</h1>
      <div id="data-freshness"></div>
    </div>
    <p>
      The badge above reads <code>build/manifest.json</code> and recomputes the
      age in the browser, so it goes stale on its own if the page is left open.
    </p>
  </main>

  <script src="freshness-badge.js"></script>
  <script>
    geoRefreshBadge.mount({
      el: '#data-freshness',
      manifest: 'build/manifest.json',
      refreshMs: 30000,
      reloadMs: 300000
    });
  </script>
</body>
</html>
"""


def _read_asset(name: str) -> str:
    return (ASSETS_DIR / name).read_text(encoding="utf-8")


def scaffold_files() -> dict[str, str]:
    """Return the ``relative path -> contents`` map that ``init`` writes."""
    return {
        "pipeline.yml": STARTER_CONFIG,
        "data/stations.csv": SAMPLE_CSV,
        "freshness-badge.js": _read_asset("freshness-badge.js"),
        "freshness-badge.css": _read_asset("freshness-badge.css"),
        "freshness-demo.html": DEMO_HTML,
    }


def write_scaffold(directory: str | Path, *, force: bool = False) -> list[str]:
    """Write the starter project into ``directory``.

    Existing files are left alone unless ``force`` is set, so re-running
    ``init`` never destroys a config someone has edited.

    Returns:
        The paths actually written, as strings.
    """
    root = Path(directory)
    written: list[str] = []
    for relative, contents in scaffold_files().items():
        target = root / relative
        if target.exists() and not force:
            continue
        atomic_write_text(target, contents)
        written.append(str(target))
    return written
