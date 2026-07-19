# geo-refresh-pipeline

A declarative, scheduled refresh pipeline for map data. You describe your sources in
YAML — HTTP APIs, local files, SQL queries — along with how to reshape them into GeoJSON
and what to build from the result. The pipeline fetches everything, transforms and
validates it, works out whether the data *actually* changed using an order-stable content
hash, and only then regenerates your artifacts. Every run writes a `manifest.json`
recording when each source was last successfully fetched, what the upstream said about its
own freshness, how many features came through, and what was added, removed or modified
since last time. A dependency-free JavaScript snippet turns that manifest into a
"Updated 3 hours ago" badge that flips to a warning when data goes stale. A composite
GitHub Action is included so a nightly workflow can deploy only when something moved.

Built alongside the notes at [geo-dashboard.com](https://www.geo-dashboard.com/).

## Features

- **Declarative YAML config** — named sources, per-source formats, transforms and outputs.
- **Three source types** — HTTP (with auth headers read from the environment), local files,
  and SQL through SQLAlchemy (an optional dependency that degrades with a clear message).
- **Real change detection** — a canonical, order-stable hash of the feature set, so a
  reordered API response or reshuffled JSON keys is *not* a change and does not trigger a
  rebuild, a commit, or a deploy.
- **Per-source diffs** — added / removed / modified counts keyed on a property you choose.
- **Freshness manifest** — per-source fetched-at, `Last-Modified`, `ETag`, feature counts,
  content hash, diff summary and a computed `stale` flag.
- **Stale-data badge** — ~4 kB of dependency-free JavaScript plus documented CSS.
- **Built for cron** — retries with exponential backoff and jitter, per-source timeouts,
  atomic writes, `--dry-run`, `--only`, `--force`, JSON logging, and exit codes that
  distinguish "nothing changed" from "the upstream is down".
- **GitHub Action** — a real composite `action.yml` with `changed` / `stale` /
  `manifest-path` outputs, plus a worked example workflow.

## Why it exists

Most map dashboards get their data from a scheduled job that re-downloads a feed, rewrites
a GeoJSON file and rebuilds a site. Three problems show up almost immediately:

1. **Everything looks like a change.** Many feeds return rows in a nondeterministic order,
   or serialise floats with a bit more precision than last time. A naive `if file != file`
   check fires every night, producing a daily commit, a daily rebuild and a daily cache
   purge for data that did not move.
2. **Failures are silent.** The feed 502s at 4am, the job exits non-zero into a log nobody
   reads, and the dashboard keeps showing week-old data as though it were live.
3. **Partial writes.** The job dies halfway through writing a 40 MB GeoJSON file and the
   live map now loads a truncated document.

This tool fixes those three specifically: content hashing that ignores ordering and
numeric spelling, a manifest the page itself can read to admit how old its data is, and
temp-file-plus-rename for every artifact it writes.

## Install and run

The tool is used by cloning this repository. It is not published to PyPI, and nothing here
assumes it is installed from a package index.

```bash
git clone https://github.com/geo-dashboard-generation/geo-refresh-pipeline.git
cd geo-refresh-pipeline

# with uv (fast):
uv venv
uv pip install -r requirements.txt

# or with plain venv:
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Then run it as a module, with `src` on the path:

```bash
PYTHONPATH=src .venv/bin/python -m geo_refresh --help
```

If you would rather not set `PYTHONPATH` on every call, install it into the virtualenv in
editable mode. This is optional and does not change how the tool behaves:

```bash
uv pip install -e .          # then: .venv/bin/geo-refresh --help
uv pip install -e '.[sql]'   # ...with SQL source support
```

`sql` sources need SQLAlchemy, which is deliberately *not* in `requirements.txt`. Install
it with `uv pip install -r requirements-sql.txt` when you need it; every other source type
works without it, and a config that uses a SQL source without it fails with an install
hint rather than a traceback.

Python 3.11 or newer is required.

## Quick start

```bash
PYTHONPATH=src .venv/bin/python -m geo_refresh init myproject
PYTHONPATH=src .venv/bin/python -m geo_refresh validate myproject/pipeline.yml
PYTHONPATH=src .venv/bin/python -m geo_refresh run myproject/pipeline.yml
```

`init` writes a runnable example: a pipeline config, 21 real air-quality monitoring
stations as CSV (actual European city coordinates), the badge assets and a demo page.

## A worked example

The scaffolded `pipeline.yml`:

```yaml
version: 1

manifest: build/manifest.json
state: build/.geo-refresh-state.json

defaults:
  timeout: 30s
  retries: 3
  retry_backoff: 500
  max_age: 24h

sources:
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

outputs:
  - geojson: build/all_features.geojson
```

First run — 21 rows in, one decommissioned station filtered out, 20 features written:

```
$ python -m geo_refresh run pipeline.yml
source.done source=air_quality_stations features=20 changed=True diff="first run, 20 features" attempts=1
output.geojson path=build/air_quality_stations.geojson features=20 source=air_quality_stations
output.summary path=build/air_quality_stations.summary.json source=air_quality_stations
output.geojson path=build/all_features.geojson features=20 source=None
manifest.written path=build/manifest.json changed=True
air_quality_stations     changed    first run, 20 features

3 output(s):
  build/air_quality_stations.geojson
  build/air_quality_stations.summary.json
  build/all_features.geojson

$ echo $?
0
```

Run it again with nothing changed upstream. No file is touched, and the exit code says so:

```
$ python -m geo_refresh run pipeline.yml
source.done source=air_quality_stations features=20 changed=False diff="no change (20 features)" attempts=1
run.no-change sources=1
manifest.written path=build/manifest.json changed=False
air_quality_stations     unchanged  no change (20 features)

Nothing changed upstream; outputs left untouched.

$ echo $?
3
```

Now edit one station's reading and append a new one:

```
$ python -m geo_refresh run pipeline.yml
source.done source=air_quality_stations features=21 changed=True diff="+1 / -0 / ~1 (of 21)" attempts=1
output.geojson path=build/air_quality_stations.geojson features=21 source=air_quality_stations
...
air_quality_stations     changed    +1 / -0 / ~1 (of 21)

$ python -m geo_refresh status build/manifest.json
manifest: build/manifest.json
generated: 2026-07-19T04:15:07Z
totals: 1 sources, 21 features, 1 changed, 0 stale

source                   age                 features  state
------------------------------------------------------------
air_quality_stations     just now                  21  changed
                         +1 / -0 / ~1
```

The manifest it wrote (abridged):

```json
{
  "schema": "geo-refresh-manifest/1",
  "generator": "geo-refresh-pipeline",
  "generated_at": "2026-07-19T04:15:07Z",
  "changed": true,
  "stale": false,
  "totals": {
    "sources": 1, "features": 21, "changed_sources": 1,
    "stale_sources": 0, "failed_sources": 0
  },
  "sources": {
    "air_quality_stations": {
      "status": "ok",
      "origin": "data/stations.csv",
      "format": "csv",
      "fetched_at": "2026-07-19T04:15:07Z",
      "age_seconds": 0,
      "last_modified": "Sun, 19 Jul 2026 04:14:52 GMT",
      "etag": null,
      "feature_count": 21,
      "content_hash": "sha256-features-v1:f62af55c0df1146090c41578df292ba72b6ba720b1e05915...",
      "previous_content_hash": "sha256-features-v1:0b1f8c4d6a2e39f5718c0d4a9e7b2c6f13a58d90...",
      "changed": true,
      "max_age_seconds": 21600.0,
      "stale": false,
      "bbox": [-9.13934, 37.98381, 24.93838, 60.16986],
      "attempts": 1,
      "duration_ms": 3,
      "bytes_read": 1247,
      "error": null,
      "diff": {
        "added": 1, "removed": 0, "modified": 1, "unchanged": 19, "total": 21,
        "keyed": true, "first_run": false, "id_property": "station_id",
        "sample": {
          "added": ["EE-TL-0029"],
          "removed": [],
          "modified": ["DE-BE-0012"]
        }
      },
      "transforms": [
        {"step": "filter", "detail": "status == 'active' and pm25 >= 0",
         "features_in": 22, "features_out": 21, "removed": 1}
      ],
      "outputs": ["build/air_quality_stations.geojson"]
    }
  }
}
```

## How it works

Each run does this, per source:

1. **Fetch** the payload with a per-source timeout, retrying transient failures with
   exponential backoff and full jitter.
2. **Parse** it into GeoJSON features according to `format` and, for JSON/CSV, `mapping`.
3. **Transform** it through the configured steps, in order.
4. **Validate** the result against `min_features`.
5. **Hash** the feature set and compare it with the hash stored by the previous run.
6. **Diff** it against the previous `id -> hash` index for the add/remove/modify counts.
7. **Write** that source's outputs — but only if it changed, or `--force` was given.

Then pipeline-level outputs run over the merged feature set, `manifest.json` is written,
and the run state is saved.

### The content hash

The hash exists so that "the bytes are different" does not get mistaken for "the data
changed". It deliberately ignores:

- **Object key order.** `{"a":1,"b":2}` and `{"b":2,"a":1}` hash identically, because JSON
  member order carries no meaning.
- **Feature order.** The collection hash is built from the *sorted* list of per-feature
  hashes. A feed that returns rows in a different order every call is not a change.
  Duplicated features still count — the sorted list is a multiset, not a set.
- **Numeric spelling.** `1`, `1.0` and `-0.0` normalise to the same token.

It deliberately does **not** ignore property values, coordinates, feature ids, or a
property appearing and disappearing. Pair it with a `round:` transform if your upstream
emits float noise in the last few decimal places — round first, then hash.

### Run state and the diff

Accurate added/removed/modified counts need to know what the previous run saw. Keeping the
whole previous dataset around is wasteful, so the pipeline persists a compact
`id -> feature hash` index in the `state:` file (one short digest per feature).

- Set `id_property:` to the property that identifies a feature across runs. Without it,
  the feature's top-level GeoJSON `id` is used.
- If neither exists, the diff degrades gracefully to a multiset comparison: `added` and
  `removed` are still exact, and `modified` is reported as `0`, because a modification is
  then indistinguishable from one removal plus one addition. The manifest sets
  `"keyed": false` so you know which mode produced the numbers.
- If the state file is missing or corrupt, the run continues and reports every feature as
  added, which is the right answer for a first run.

In CI, commit the state file (or cache it) if you want diffs that span jobs. The
content-hash change detection works without it, because the hash also lives in the
manifest.

### Failure behaviour

Two rules decide what a partly broken upstream does to a live dashboard:

- **If any source fails, pipeline-level outputs and build commands are skipped.**
  Publishing half a dataset is worse than publishing yesterday's.
- **The manifest is written anyway**, carrying forward each failed source's *last
  successful* `fetched_at`. That is what makes the badge go stale on its own: a source
  that has been failing for a day looks a day old, without any extra plumbing.

Every artifact is written to a temporary file in the destination directory and then
renamed over the target, so a crash mid-write never leaves a half-written GeoJSON file
where the map expects a whole one.

## CLI reference

```
python -m geo_refresh <command> [options]
```

Global: `--version`, `--help`. Every command accepts `--log-format {text,json}` and
`--log-level {debug,info,warning,error,silent}`. Logs go to stderr; command output goes to
stdout.

### `run CONFIG`

Fetch every source and regenerate artifacts when the content changed.

| Option | Meaning |
| --- | --- |
| `--only SOURCE` | Run only this source. Repeat for several. Pipeline-level *file* outputs are skipped when only a subset ran, since they would contain incomplete data. |
| `--force` | Write outputs and run build commands even when nothing changed. |
| `--dry-run` | Fetch, transform and diff, but write nothing and run no build command. |
| `--skip-build` | Write artifacts but skip every `build` output. |
| `--summary PATH` | Append `key=value` lines (`changed`, `stale`, `manifest-path`, `feature-count`, `exit-code`) to a file. This is how the GitHub Action populates its step outputs; point it at `$GITHUB_OUTPUT`. |

### `validate CONFIG`

Parse the config, compile every filter expression and print the plan. Makes no network,
filesystem or database calls, so it is safe to run in a pre-commit hook or on a machine
without the secrets.

### `status [MANIFEST]`

Pretty-print a manifest. Accepts either a `manifest.json` path or a config file, in which
case it reads the manifest path out of the config. `--json` emits the raw document on
stdout for piping. Exits `5` if any source is stale, which makes it usable as a monitoring
check.

### `init [DIRECTORY]`

Write the starter project. Existing files are left alone unless `--force` is given.

### Exit codes

| Code | Name | Meaning |
| --- | --- | --- |
| `0` | ok | At least one source changed (or `--force`) and everything succeeded. |
| `3` | no-change | Every source hashed identically to the previous run. Nothing was written. |
| `4` | fetch-failure | A source could not be read after its retries. |
| `5` | validation-failure | A payload was malformed, or failed `min_features`. |
| `6` | config-error | The config file is missing or invalid. |
| `7` | output-failure | A write or a build command failed. |

Treat `3` as success in a scheduled job — see the action's handling below.

## Configuration reference

### Top level

| Key | Default | Meaning |
| --- | --- | --- |
| `version` | `1` | Config schema version. |
| `manifest` | `manifest.json` | Where to write the freshness manifest. |
| `state` | `.geo-refresh-state.json` | Where to persist the per-source diff index. |
| `defaults` | see below | Per-source settings applied when a source omits them. |
| `sources` | *required* | One or more named sources. |
| `outputs` | `[]` | Outputs run once over every source's features merged together. |

Relative paths are resolved against the config file's own directory, so a pipeline can be
run from anywhere.

### `defaults`

| Key | Default | Meaning |
| --- | --- | --- |
| `timeout` | `30s` | Per-request timeout. |
| `retries` | `3` | Retries after the first attempt (so 4 attempts total). |
| `retry_backoff` | `0.5` | Base backoff in seconds. Delay before attempt *n* is uniform over `[0, min(retry_max_backoff, retry_backoff · 2ⁿ⁻²)]`. |
| `retry_max_backoff` | `30s` | Cap on the backoff window. |
| `max_age` | unset | Age after which a source counts as stale. Unset means never stale. |
| `min_features` | `0` | Minimum surviving features, else the run fails validation. |

Durations accept a bare number of seconds or a suffixed string: `90s`, `15m`, `6h`, `7d`,
`2w`.

### Sources

Common keys, valid on every source type:

| Key | Meaning |
| --- | --- |
| `name` | Required. Letters, digits, `_`, `.`, `-`. Unique within the pipeline. |
| `type` | `http`, `file` or `sql`. Inferred from `url` / `path` / `query` when omitted. |
| `format` | `geojson` (default), `json` or `csv`. |
| `mapping` | Required for `json` and `csv`; forbidden for `geojson`. See below. |
| `id_property` | Feature property used as the diff key. |
| `transform` | List of transform steps, applied in order. |
| `outputs` | Outputs for this source alone. |
| `max_age`, `timeout`, `retries`, `retry_backoff`, `min_features` | Override the defaults. |

`type: http`:

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | *required* | Absolute `http://` or `https://` URL. |
| `method` | `GET` | `GET` or `POST`. |
| `headers` | `{}` | Request headers. Values may reference the environment. |
| `params` | `{}` | Query parameters. |
| `body` | none | Request body for `POST`. |
| `verify_tls` | `true` | Set to `false` only for an internal host with a private CA. |

`type: file`:

| Key | Default | Meaning |
| --- | --- | --- |
| `path` | *required* | File path, relative to the config file. |
| `encoding` | `utf-8` | Text encoding of the file. |

The file's mtime becomes the manifest's `last_modified`.

`type: sql`:

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | *required* | SQLAlchemy database URL. Credentials are redacted before they reach a log or the manifest. |
| `query` | *required* | A single SQL statement returning one row per feature. |

### Environment substitution

Any string in the config may reference the environment as `${NAME}` or
`${NAME:-fallback}`; `$$` is a literal `$`. Substitution happens when the source is
*used*, not when the config is parsed, which is why `validate` works on a machine without
the secrets. A missing variable with no fallback is an error naming the variable.

```yaml
headers:
  Authorization: "Bearer ${INCIDENTS_TOKEN}"
  X-Client: "${CLIENT_ID:-dashboard}"
```

### `mapping`

Turns arbitrary JSON or CSV records into features.

| Key | Default | Meaning |
| --- | --- | --- |
| `records` | document root | Selector for the record array, e.g. `$.data.stations`. JSON only. |
| `geometry` | *required* | See below. |
| `properties` | `"*"` | List of fields to carry over, or `"*"` for all non-geometry fields. |
| `id` | none | Record field copied to the feature's top-level `id`. |

Geometry, either from two scalar fields:

```yaml
geometry: { type: point, lon: longitude, lat: latitude }
```

or from a field already holding a GeoJSON geometry object (or a JSON string of one):

```yaml
geometry: { type: geometry, field: shape }
```

The `records` selector is a small JSONPath subset: `$` for the root, `.name` for members,
`["odd.name"]` for members containing dots, `[0]` for an index and `[*]` for every element.
A missing key raises an error listing the keys that *are* there, rather than silently
selecting nothing.

CSV is parsed with a sniffed delimiter (`,`, `;`, tab or `|`) and a required header row.
Cells are coerced to int/float/bool/null only when the value round-trips exactly, so
identifiers like `01234` stay strings.

### Transform steps

Each step is one entry in the `transform:` list, written as a single-key mapping:

| Step | Example | Effect |
| --- | --- | --- |
| `filter` | `- filter: "capacity > 0 and status == 'active'"` | Keep features whose properties satisfy the expression. |
| `select` | `- select: [id, name, pm25]` | Keep only these properties. Absent names are skipped, not an error. |
| `rename` | `- rename: {pm25: pm25_ug_m3}` | Rename properties; unlisted ones are untouched. |
| `round` | `- round: 5` | Round every coordinate to N decimal places. |
| `drop_invalid` | `- drop_invalid: true` | Drop features whose geometry is missing or structurally invalid. |

Filter expressions look like Python but are compiled against an explicit whitelist: no
attribute access, no subscripting, no imports, and no calls outside the built-in function
list. A config file cannot reach the host through a filter.

Bare names resolve to feature properties, and an unknown property evaluates to `null`
rather than raising, so an optional field that a feed sometimes omits fails the comparison
instead of aborting the run. `null`, `true` and `false` are accepted alongside their
Python spellings. Available functions: `len`, `lower`, `upper`, `abs`, `int`, `float`,
`str`, `bool`, `startswith`, `endswith`, `contains`, `is_null`, `coalesce`.

`round` is worth applying before you rely on change detection: it is the difference
between a nightly rebuild and a nightly no-op when an upstream re-serialises its floats.

### Outputs

Outputs are a list, written as single-key mappings. They can appear on a source (running
over that source's features) or at the top level (running over every source's features
merged, once, and only when at least one source changed).

| Output | Example | Effect |
| --- | --- | --- |
| `geojson` | `- geojson: build/out.geojson` | Write a `FeatureCollection`. Add `indent: 2` for a readable, diff-friendly file. |
| `summary` | `- summary: build/out.summary.json` | Write feature count, bbox, geometry-type histogram, property names and content hash. |
| `build` | `- build: "python render.py"` | Run a shell command. Add `cwd:`, `env:` or `timeout:` (default 15 minutes). |

Build commands run last, after every artifact is on disk. On a non-zero exit the last 20
lines of its output are quoted back in the error.

## The stale-data badge

`src/geo_refresh/assets/freshness-badge.js` and `freshness-badge.css` are copied into your
project by `init`. No build step, no dependencies, ~4 kB.

```html
<link rel="stylesheet" href="freshness-badge.css">

<div class="map-header">
  <h1>Air quality stations</h1>
  <div id="data-freshness"></div>
</div>

<script src="freshness-badge.js"></script>
<script>
  geoRefreshBadge.mount({
    el: '#data-freshness',
    manifest: '/build/manifest.json',
    refreshMs: 30000,     // redraw the relative time every 30s
    reloadMs: 300000      // re-fetch the manifest every 5 minutes
  });
</script>
```

The badge recomputes the age in the browser from `fetched_at` rather than trusting the
manifest's `stale` flag alone, so a dashboard left open overnight goes stale on its own
without a reload.

### `mount(options)`

| Option | Default | Meaning |
| --- | --- | --- |
| `el` | *required* | Target element or CSS selector. |
| `manifest` | `manifest.json` | Manifest URL. |
| `source` | all | Report only this source instead of the oldest across the manifest. |
| `refreshMs` | `60000` | How often to redraw the relative time. `0` draws once. |
| `reloadMs` | `0` | How often to re-fetch the manifest. `0` disables it. |
| `onUpdate` | none | Called with the badge model after each draw. |

It returns `{ stop(), refresh() }`. Also exported: `geoRefreshBadge.evaluate(manifest,
options)` returns the model without touching the DOM, if you would rather render the state
yourself, and `geoRefreshBadge.humanizeAge(seconds)` matches the Python `humanize_age`
exactly (the test suite asserts this under Node).

### States and styling

The badge sets `data-state` on its element, so you can restyle it entirely by targeting
that attribute:

| State | Rendered as |
| --- | --- |
| `fresh` | "Updated 3 hours ago" |
| `stale` | "Data may be out of date — updated 2 days ago" |
| `failed` | "Refresh failed — showing data from 6 hours ago", with the error in the tooltip |
| `unknown` | "Freshness unknown" / "Loading…" |

```css
.geo-refresh-badge[data-state="stale"] {
  background: #fdf3e0;
  border-color: #e6c47a;
  color: #6b4708;
}
```

The shipped CSS covers all four states in light and dark schemes, and pulses the stale and
failed icons unless the visitor prefers reduced motion. `freshness-demo.html`, also written
by `init`, is a complete working page.

## GitHub Action

`action.yml` at the repository root is a composite action.

```yaml
- name: Refresh
  id: refresh
  uses: geo-dashboard-generation/geo-refresh-pipeline@v1
  with:
    config: pipeline.yml
    python-version: '3.12'
    log-format: json
  env:
    INCIDENTS_TOKEN: ${{ secrets.INCIDENTS_TOKEN }}
```

Inputs: `config` (required), `python-version` (default `3.12`), `working-directory`,
`args` (extra CLI arguments such as `--force` or `--only bike_stations`), `log-format`
(default `json`), `install-sql-extra` (set to `true` if you use a `sql` source), and
`fail-on-stale`.

Outputs: `changed`, `stale`, `manifest-path`, `feature-count` and the raw `exit-code`.

The action treats exit code `3` as success, because "nothing changed upstream" is a normal
outcome for a scheduled refresh, not a build failure. Gate your publishing steps on
`changed` instead:

```yaml
- name: Commit regenerated artifacts
  if: steps.refresh.outputs.changed == 'true'
  run: |
    git config user.name 'github-actions[bot]'
    git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
    git add build/
    git commit -m "Refresh map data"
    git push
```

A complete workflow — nightly schedule, `repository_dispatch` for webhook-triggered
refreshes, commit-and-deploy-to-Pages only when `changed == 'true'` — is in
[examples/workflows/refresh-map-data.yml](examples/workflows/refresh-map-data.yml). It
lives under `examples/` rather than `.github/workflows/` so it does not run nightly against
a pipeline this repository does not own; copy it into the repository that holds your map
data.

## Library use

The Python API is small and stable:

```python
from geo_refresh import RunOptions, load_config, run_pipeline

config = load_config("pipeline.yml")
result = run_pipeline(config, RunOptions(dry_run=True))

print(result.changed, result.stale, result.exit_code)
for entry in result.manifest.sources:
    print(entry.name, entry.feature_count, entry.diff.describe())
```

`run_pipeline` never raises for an upstream failure — it records the failure in the
manifest and returns a non-zero `exit_code`. It *does* raise `ConfigError` for a broken
config and `OutputError` if writing an artifact fails.

## Development

```bash
uv pip install -r requirements-dev.txt
PYTHONPATH=src .venv/bin/python -m pytest
```

The suite runs in well under a second and touches neither the network nor a real database:
HTTP is stubbed with httpx's `MockTransport`, and the SQL source is tested against SQLite.
The badge JavaScript is exercised under Node (those tests skip if `node` is absent), and
`action.yml` is validated against a schema for the GitHub Actions metadata syntax.

CI runs the suite on Python 3.11, 3.12 and 3.13, plus a job that installs *without*
SQLAlchemy to prove the optional import degrades correctly.

## Limitations

- **No reprojection.** Everything is assumed to be WGS84 lon/lat (EPSG:4326), which is
  what GeoJSON requires. If your source is in a projected CRS, reproject it in a `build`
  step with pyproj or geopandas before this pipeline sees it.
- **No topological validation.** `drop_invalid` checks structure — nesting depth, numeric
  positions, closed rings, coordinates in range — not self-intersection or ring winding
  order. Use shapely in a `build` step if you need that.
- **Whole-payload fetches.** There is no incremental/partial fetch: each run downloads the
  full source and diffs it locally. That is the right trade for feeds up to a few hundred
  MB; past that, have the upstream expose a change feed.
- **In-memory processing.** The whole feature set is held in memory. Millions of features
  will want a streaming tool instead.
- **One statement per SQL source.** `query` is executed as a single statement; build
  temporary tables in a view or a `build` step.
- **The diff index grows with feature count.** One 64-character digest per feature. At a
  million features the state file is roughly 80 MB, at which point you probably want to
  drop `id_property` and accept a hash-only diff.
- **`build` commands run through a shell.** That is deliberate so `&&` and pipes work as
  written, but it means the config file is as trusted as a shell script. Do not run a
  config you did not write.

## Further reading

These guides cover the surrounding decisions this tool assumes you have already made —
scheduling, triggering, and what to do with the artifacts once they change:

- [Automating nightly GeoJSON rebuilds with GitHub Actions](https://www.geo-dashboard.com/data-refresh-automation-pipelines/scheduled-map-rebuild-workflows/automating-nightly-geojson-rebuilds-with-github-actions/)
- [Rebuilding tiles on GitHub push with repository dispatch](https://www.geo-dashboard.com/data-refresh-automation-pipelines/webhook-triggered-updates/rebuilding-tiles-on-github-push-with-repository-dispatch/)
- [Computing GeoJSON feature deltas with GeoPandas](https://www.geo-dashboard.com/data-refresh-automation-pipelines/incremental-data-processing/computing-geojson-feature-deltas-with-geopandas/)
- [Choosing cron vs APScheduler for single-server dashboards](https://www.geo-dashboard.com/data-refresh-automation-pipelines/scheduler-selection-celery-apscheduler-cron/choosing-cron-vs-apscheduler-for-single-server-dashboards/)
- [Deploying generated map bundles with GitHub Actions CI/CD](https://www.geo-dashboard.com/python-to-web-generation-workflows/static-vs-dynamic-export-methods/deploying-generated-map-bundles-with-github-actions-cicd/)

Those notes live at [geo-dashboard.com](https://www.geo-dashboard.com/), which is where
this tool's design came from.

## License

MIT — see [LICENSE](LICENSE).
