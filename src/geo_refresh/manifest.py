"""The freshness manifest.

``manifest.json`` is the pipeline's public contract with whatever renders the
map. It records, per source, when the data was last *successfully* fetched, what
the upstream said about its own freshness, how many features came out, the
content hash, the diff against the previous run, and whether the data is now
older than its configured ``max_age``.

The important subtlety: ``fetched_at`` is the last **successful** fetch, not the
last run. A source that fails to refresh keeps its old timestamp and therefore
goes stale on its own, which is exactly the signal a "data may be out of date"
badge needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .diffing import DiffSummary
from .errors import ValidationError

#: Schema identifier written into every manifest.
MANIFEST_SCHEMA = "geo-refresh-manifest/1"

#: Status values a source entry can take.
STATUS_OK = "ok"
STATUS_UNCHANGED = "unchanged"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def iso8601(moment: datetime) -> str:
    """Format an aware datetime as ``2026-07-19T08:30:00Z``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso8601(text: str) -> datetime:
    """Parse an ISO 8601 timestamp, accepting a trailing ``Z``.

    Raises:
        ValidationError: If the timestamp cannot be parsed.
    """
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"not a valid ISO 8601 timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def humanize_age(seconds: float) -> str:
    """Render an age as ``"3 hours ago"`` / ``"just now"``.

    Mirrors the logic in the browser badge so the CLI and the page agree.
    """
    if seconds < 0:
        return "in the future"
    if seconds < 45:
        return "just now"
    units = (
        (86400 * 365, "year"),
        (86400 * 30, "month"),
        (86400 * 7, "week"),
        (86400, "day"),
        (3600, "hour"),
        (60, "minute"),
    )
    for size, name in units:
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {name}{'s' if count != 1 else ''} ago"
    return f"{int(seconds)} seconds ago"


def compute_stale(
    fetched_at: datetime | str | None, max_age: float | None, *, now: datetime | None = None
) -> bool:
    """Whether data fetched at ``fetched_at`` has exceeded ``max_age`` seconds.

    Returns ``False`` when no ``max_age`` is configured (the user opted out of
    staleness for that source) and ``True`` when the timestamp is missing but a
    ``max_age`` is set (never-fetched data is stale by definition).
    """
    if max_age is None:
        return False
    if fetched_at is None:
        return True
    moment = parse_iso8601(fetched_at) if isinstance(fetched_at, str) else fetched_at
    reference = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (reference - moment) > timedelta(seconds=max_age)


@dataclass
class SourceManifest:
    """The manifest entry for one source."""

    name: str
    status: str = STATUS_OK
    origin: str | None = None
    format: str | None = None
    fetched_at: str | None = None
    last_modified: str | None = None
    etag: str | None = None
    feature_count: int = 0
    content_hash: str | None = None
    previous_content_hash: str | None = None
    changed: bool = False
    max_age_seconds: float | None = None
    stale: bool = False
    bbox: list[float] | None = None
    attempts: int = 1
    duration_ms: int = 0
    bytes_read: int = 0
    error: str | None = None
    error_code: int | None = None
    diff: DiffSummary | None = None
    transforms: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since the last successful fetch, or ``None`` if never."""
        if self.fetched_at is None:
            return None
        return max(0.0, ((now or utcnow()) - parse_iso8601(self.fetched_at)).total_seconds())

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        """JSON-serialisable form, including the derived age."""
        age = self.age_seconds(now)
        return {
            "status": self.status,
            "origin": self.origin,
            "format": self.format,
            "fetched_at": self.fetched_at,
            "age_seconds": None if age is None else int(age),
            "last_modified": self.last_modified,
            "etag": self.etag,
            "feature_count": self.feature_count,
            "content_hash": self.content_hash,
            "previous_content_hash": self.previous_content_hash,
            "changed": self.changed,
            "max_age_seconds": self.max_age_seconds,
            "stale": self.stale,
            "bbox": self.bbox,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "bytes_read": self.bytes_read,
            "error": self.error,
            "error_code": self.error_code,
            "diff": self.diff.to_dict() if self.diff else None,
            "transforms": self.transforms,
            "outputs": self.outputs,
        }


@dataclass
class Manifest:
    """The whole manifest document."""

    generated_at: datetime = field(default_factory=utcnow)
    sources: list[SourceManifest] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        """Whether any source's content hash moved this run."""
        return any(source.changed for source in self.sources)

    @property
    def stale(self) -> bool:
        """Whether any source is past its ``max_age``."""
        return any(source.stale for source in self.sources)

    @property
    def failed(self) -> list[SourceManifest]:
        """Sources whose fetch or validation failed."""
        return [source for source in self.sources if source.status == STATUS_FAILED]

    @property
    def feature_count(self) -> int:
        """Total features across all sources."""
        return sum(source.feature_count for source in self.sources)

    def get(self, name: str) -> SourceManifest | None:
        """Return the entry for ``name``, or ``None``."""
        for source in self.sources:
            if source.name == name:
                return source
        return None

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        """JSON-serialisable form of the whole manifest."""
        reference = now or utcnow()
        return {
            "schema": MANIFEST_SCHEMA,
            "generator": "geo-refresh-pipeline",
            "generated_at": iso8601(self.generated_at),
            "changed": self.changed,
            "stale": self.stale,
            "dry_run": self.dry_run,
            "totals": {
                "sources": len(self.sources),
                "features": self.feature_count,
                "changed_sources": sum(1 for s in self.sources if s.changed),
                "stale_sources": sum(1 for s in self.sources if s.stale),
                "failed_sources": len(self.failed),
            },
            "sources": {s.name: s.to_dict(reference) for s in self.sources},
            "outputs": self.outputs,
        }


def load_manifest_document(path: str | Path) -> dict[str, Any]:
    """Read a manifest file into a plain dict.

    Raises:
        ValidationError: If the file is missing or not a valid manifest.
    """
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise ValidationError(
            f"no manifest at {manifest_path}. Run the pipeline once to create it."
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"cannot read {manifest_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "sources" not in payload:
        raise ValidationError(
            f"{manifest_path} does not look like a geo-refresh manifest "
            f"(no 'sources' member)"
        )
    return payload
