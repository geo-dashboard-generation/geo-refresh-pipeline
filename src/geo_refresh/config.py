"""Declarative pipeline configuration, parsed and validated with pydantic.

The public entry point is :func:`load_config`, which reads a YAML file and
returns a fully validated :class:`PipelineConfig`. Every validation failure is
re-raised as :class:`~geo_refresh.errors.ConfigError` with a message that names
the offending field, because the CLI surfaces it directly to the user.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError as PydanticValidationError,
    field_validator,
    model_validator,
)

from .errors import ConfigError

# --------------------------------------------------------------------------- #
# Duration helpers
# --------------------------------------------------------------------------- #

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 1}


def parse_duration(value: str | int | float) -> float:
    """Parse ``"90s"``, ``"6h"``, ``"7d"`` or a bare number into seconds.

    A bare number is interpreted as seconds.

    Raises:
        ValueError: If the string is not a recognised duration.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds < 0:
            raise ValueError("duration must not be negative")
        return seconds
    match = _DURATION_RE.match(str(value))
    if not match:
        raise ValueError(
            f"invalid duration {value!r}; use a number of seconds or a suffixed "
            f"value such as '30s', '15m', '6h', '7d'"
        )
    return float(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]


Duration = Annotated[float, Field(ge=0)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# --------------------------------------------------------------------------- #
# Transform steps
# --------------------------------------------------------------------------- #


class FilterStep(_Base):
    """Keep only features whose properties satisfy a boolean expression."""

    op: Literal["filter"] = "filter"
    expression: str = Field(min_length=1)


class SelectStep(_Base):
    """Keep only the named properties on each feature."""

    op: Literal["select"] = "select"
    properties: list[str] = Field(min_length=1)


class RenameStep(_Base):
    """Rename properties. Keys are existing names, values are the new names."""

    op: Literal["rename"] = "rename"
    mapping: dict[str, str] = Field(min_length=1)


class RoundStep(_Base):
    """Round every coordinate to ``precision`` decimal places."""

    op: Literal["round"] = "round"
    precision: int = Field(ge=0, le=15)


class DropInvalidStep(_Base):
    """Drop features whose geometry is missing or structurally invalid."""

    op: Literal["drop_invalid"] = "drop_invalid"
    enabled: bool = True


TransformStep = Annotated[
    Union[FilterStep, SelectStep, RenameStep, RoundStep, DropInvalidStep],
    Field(discriminator="op"),
]

_SHORTHAND_TRANSFORMS: dict[str, tuple[type[_Base], str]] = {
    "filter": (FilterStep, "expression"),
    "select": (SelectStep, "properties"),
    "rename": (RenameStep, "mapping"),
    "round": (RoundStep, "precision"),
    "drop_invalid": (DropInvalidStep, "enabled"),
}


def _normalise_transform(raw: Any) -> Any:
    """Accept the single-key shorthand (``{filter: "pop > 10"}``) as well as
    the explicit ``{op: filter, expression: "pop > 10"}`` form."""
    if not isinstance(raw, dict) or "op" in raw:
        return raw
    if len(raw) != 1:
        raise ConfigError(
            f"transform step {raw!r} is ambiguous: use one key per step "
            f"(one of {', '.join(sorted(_SHORTHAND_TRANSFORMS))}), or the "
            f"explicit {{op: ..., ...}} form"
        )
    (key, value), = raw.items()
    if key not in _SHORTHAND_TRANSFORMS:
        raise ConfigError(
            f"unknown transform step {key!r}; expected one of "
            f"{', '.join(sorted(_SHORTHAND_TRANSFORMS))}"
        )
    _, field = _SHORTHAND_TRANSFORMS[key]
    return {"op": key, field: value}


# --------------------------------------------------------------------------- #
# Geometry mapping (for json / csv sources)
# --------------------------------------------------------------------------- #


class PointMapping(_Base):
    """Build a Point geometry from two scalar fields."""

    type: Literal["point"] = "point"
    lon: str = Field(min_length=1, description="Field holding longitude (x).")
    lat: str = Field(min_length=1, description="Field holding latitude (y).")


class GeometryFieldMapping(_Base):
    """Take an embedded GeoJSON geometry object from a single field."""

    type: Literal["geometry"] = "geometry"
    field: str = Field(min_length=1)


GeometryMapping = Annotated[
    Union[PointMapping, GeometryFieldMapping], Field(discriminator="type")
]


class RecordMapping(_Base):
    """How to turn arbitrary JSON/CSV records into GeoJSON features."""

    records: str | None = Field(
        default=None,
        description=(
            "JSONPath-ish selector for the record list, e.g. '$.data.stations'. "
            "Ignored for CSV. Defaults to the document root."
        ),
    )
    geometry: GeometryMapping
    properties: list[str] | Literal["*"] = Field(
        default="*",
        description="Property fields to carry over; '*' keeps all non-geometry fields.",
    )
    id: str | None = Field(
        default=None, description="Record field to copy into the feature's top-level id."
    )


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

SourceFormat = Literal["geojson", "json", "csv"]


class _SourceBase(_Base):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    format: SourceFormat = "geojson"
    mapping: RecordMapping | None = None
    id_property: str | None = Field(
        default=None,
        description=(
            "Feature property used to key the added/removed/modified diff. "
            "Falls back to the feature's top-level id."
        ),
    )
    transform: list[TransformStep] = Field(default_factory=list)
    outputs: list["Output"] = Field(default_factory=list)
    max_age: Duration | None = None
    timeout: Duration | None = None
    retries: int | None = Field(default=None, ge=0, le=10)
    retry_backoff: Duration | None = None
    min_features: int = Field(
        default=0,
        ge=0,
        description="Fail validation if fewer than this many features survive transforms.",
    )

    @field_validator("max_age", "timeout", "retry_backoff", mode="before")
    @classmethod
    def _coerce_duration(cls, value: Any) -> Any:
        if value is None:
            return None
        return parse_duration(value)

    @field_validator("transform", mode="before")
    @classmethod
    def _coerce_transforms(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [_normalise_transform(item) for item in value]
        return value

    @model_validator(mode="after")
    def _mapping_required_for_tabular(self) -> "_SourceBase":
        if self.format in ("json", "csv") and self.mapping is None:
            raise ValueError(
                f"source '{self.name}' has format '{self.format}' and therefore needs a "
                f"'mapping:' block describing how to build geometry from each record"
            )
        if self.format == "geojson" and self.mapping is not None:
            raise ValueError(
                f"source '{self.name}' has format 'geojson'; a 'mapping:' block is only "
                f"meaningful for 'json' and 'csv'"
            )
        return self


class HttpSource(_SourceBase):
    """Fetch a document over HTTP(S)."""

    type: Literal["http"] = "http"
    url: str = Field(min_length=1)
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    verify_tls: bool = True

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, value: str) -> str:
        if not value.startswith(("http://", "https://", "${")):
            raise ValueError("http source url must start with http:// or https://")
        return value


class FileSource(_SourceBase):
    """Read a document from the local filesystem."""

    type: Literal["file"] = "file"
    path: str = Field(min_length=1)
    encoding: str = "utf-8"


class SqlSource(_SourceBase):
    """Run a query against a database via SQLAlchemy."""

    type: Literal["sql"] = "sql"
    url: str = Field(min_length=1, description="SQLAlchemy database URL.")
    query: str = Field(min_length=1)

    @model_validator(mode="after")
    def _sql_needs_mapping(self) -> "SqlSource":
        if self.mapping is None:
            raise ValueError(
                f"source '{self.name}' is a sql source and needs a 'mapping:' block "
                f"describing which columns hold the geometry"
            )
        return self


Source = Annotated[Union[HttpSource, FileSource, SqlSource], Field(discriminator="type")]


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


class GeoJsonOutput(_Base):
    """Write the feature collection as a GeoJSON file."""

    type: Literal["geojson"] = "geojson"
    path: str = Field(min_length=1)
    indent: int | None = Field(default=None, ge=0, le=8)


class SummaryOutput(_Base):
    """Write a small JSON summary (counts, bbox, property names)."""

    type: Literal["summary"] = "summary"
    path: str = Field(min_length=1)


class BuildOutput(_Base):
    """Run a shell command, typically the site/tile build."""

    type: Literal["build"] = "build"
    command: str = Field(min_length=1)
    cwd: str | None = None
    timeout: Duration = 900.0
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("timeout", mode="before")
    @classmethod
    def _coerce_timeout(cls, value: Any) -> Any:
        return parse_duration(value)


Output = Annotated[
    Union[GeoJsonOutput, SummaryOutput, BuildOutput], Field(discriminator="type")
]

_SHORTHAND_OUTPUTS: dict[str, tuple[str, str]] = {
    "geojson": ("geojson", "path"),
    "summary": ("summary", "path"),
    "build": ("build", "command"),
}


def _normalise_output(raw: Any) -> Any:
    """Accept ``{geojson: build/out.geojson}`` alongside the explicit form."""
    if not isinstance(raw, dict) or "type" in raw:
        return raw
    keys = set(raw) & set(_SHORTHAND_OUTPUTS)
    if len(keys) != 1:
        raise ConfigError(
            f"output {raw!r} must name exactly one of "
            f"{', '.join(sorted(_SHORTHAND_OUTPUTS))}, or use the explicit "
            f"{{type: ..., ...}} form"
        )
    key = keys.pop()
    kind, field = _SHORTHAND_OUTPUTS[key]
    rest = {k: v for k, v in raw.items() if k != key}
    return {"type": kind, field: raw[key], **rest}


# --------------------------------------------------------------------------- #
# Defaults + top-level config
# --------------------------------------------------------------------------- #


class Defaults(_Base):
    """Per-source settings applied when a source does not override them."""

    timeout: Duration = 30.0
    retries: int = Field(default=3, ge=0, le=10)
    retry_backoff: Duration = 0.5
    retry_max_backoff: Duration = 30.0
    max_age: Duration | None = None
    min_features: int = Field(default=0, ge=0)

    @field_validator(
        "timeout", "retry_backoff", "retry_max_backoff", "max_age", mode="before"
    )
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return None if value is None else parse_duration(value)


class PipelineConfig(_Base):
    """A whole refresh pipeline."""

    version: int = Field(default=1, ge=1, le=1)
    manifest: str = Field(default="manifest.json", min_length=1)
    state: str = Field(
        default=".geo-refresh-state.json",
        min_length=1,
        description=(
            "Compact per-source id->hash index used to compute the add/remove/modify "
            "diff between runs. Commit it in CI to get diffs across jobs."
        ),
    )
    defaults: Defaults = Field(default_factory=Defaults)
    sources: list[Source] = Field(min_length=1)
    outputs: list[Output] = Field(default_factory=list)

    # Resolved at load time so relative paths in the config are relative to it.
    base_dir: Path = Field(default=Path("."), exclude=True)

    @field_validator("sources", mode="before")
    @classmethod
    def _default_source_type(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [_infer_source_type(item) for item in value]

    @field_validator("outputs", mode="before")
    @classmethod
    def _coerce_outputs(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [_normalise_output(item) for item in value]
        return value

    @model_validator(mode="after")
    def _unique_source_names(self) -> "PipelineConfig":
        seen: set[str] = set()
        for source in self.sources:
            if source.name in seen:
                raise ValueError(f"duplicate source name '{source.name}'")
            seen.add(source.name)
        return self

    def resolve(self, path: str) -> Path:
        """Resolve a config-relative path against the config file's directory."""
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate
        return (self.base_dir / candidate).resolve()

    def source_by_name(self, name: str) -> Source | None:
        """Return the named source, or ``None``."""
        for source in self.sources:
            if source.name == name:
                return source
        return None

    def effective(self, source: Source, field: str) -> Any:
        """Return ``source.<field>``, falling back to the pipeline defaults."""
        value = getattr(source, field, None)
        if value is None:
            return getattr(self.defaults, field, None)
        return value


def _infer_source_type(raw: Any) -> Any:
    """Infer the discriminator when it is omitted but obvious."""
    if not isinstance(raw, dict) or "type" in raw:
        if isinstance(raw, dict):
            raw = dict(raw)
            if isinstance(raw.get("outputs"), list):
                raw["outputs"] = [_normalise_output(o) for o in raw["outputs"]]
        return raw
    raw = dict(raw)
    if isinstance(raw.get("outputs"), list):
        raw["outputs"] = [_normalise_output(o) for o in raw["outputs"]]
    if "path" in raw:
        raw["type"] = "file"
    elif "query" in raw:
        raw["type"] = "sql"
    elif "url" in raw:
        raw["type"] = "http"
    return raw


def _format_pydantic_error(exc: PydanticValidationError, origin: str) -> str:
    lines = [f"{origin} is not a valid pipeline configuration:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def parse_config(data: Any, *, base_dir: Path | str = ".", origin: str = "config") -> PipelineConfig:
    """Validate an already-loaded mapping into a :class:`PipelineConfig`.

    Raises:
        ConfigError: With a multi-line, field-by-field explanation.
    """
    if data is None:
        raise ConfigError(f"{origin} is empty; it must define at least a 'sources:' list")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{origin} must be a YAML mapping at the top level, got {type(data).__name__}"
        )
    payload = dict(data)
    payload["base_dir"] = Path(base_dir)
    try:
        return PipelineConfig.model_validate(payload)
    except PydanticValidationError as exc:
        raise ConfigError(_format_pydantic_error(exc, origin)) from exc
    except ValueError as exc:  # duration parsing etc.
        raise ConfigError(f"{origin}: {exc}") from exc


def load_config(path: str | Path) -> PipelineConfig:
    """Read and validate a YAML pipeline configuration from disk.

    Relative paths inside the config are resolved against the config's own
    directory, so a pipeline can be run from anywhere.

    Raises:
        ConfigError: If the file is missing, is not valid YAML, or fails
            validation.
    """
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise ConfigError(
            f"configuration file not found: {config_path}. "
            f"Run 'python -m geo_refresh init' to create a starter config."
        )
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc
    return parse_config(data, base_dir=config_path.parent, origin=str(config_path))


# Resolve the forward reference on the source models.
HttpSource.model_rebuild()
FileSource.model_rebuild()
SqlSource.model_rebuild()
PipelineConfig.model_rebuild()
