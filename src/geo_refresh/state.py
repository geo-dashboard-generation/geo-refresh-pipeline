"""Persistent run state: the id->hash index each source needs to diff.

The state file is small (one short hex digest per feature) and is the only
thing that has to survive between runs to get an accurate add/remove/modify
diff. A missing or corrupt state file is not fatal: the run continues and every
feature is reported as added, which is the correct answer for a first run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json

#: Bumped when the on-disk layout changes incompatibly.
STATE_VERSION = 1


@dataclass
class SourceState:
    """One source's persisted state."""

    content_hash: str | None = None
    index: dict[str, str] = field(default_factory=dict)
    fetched_at: str | None = None
    feature_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "content_hash": self.content_hash,
            "fetched_at": self.fetched_at,
            "feature_count": self.feature_count,
            "index": self.index,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "SourceState":
        """Parse one source entry, tolerating unexpected shapes."""
        if not isinstance(payload, dict):
            return cls()
        index = payload.get("index")
        if not isinstance(index, dict):
            index = {}
        return cls(
            content_hash=payload.get("content_hash")
            if isinstance(payload.get("content_hash"), str)
            else None,
            index={str(k): str(v) for k, v in index.items()},
            fetched_at=payload.get("fetched_at")
            if isinstance(payload.get("fetched_at"), str)
            else None,
            feature_count=payload["feature_count"]
            if isinstance(payload.get("feature_count"), int)
            else len(index),
        )


@dataclass
class PipelineState:
    """State for every source in a pipeline."""

    sources: dict[str, SourceState] = field(default_factory=dict)
    #: True when the file was absent or unreadable, i.e. this is a first run.
    fresh: bool = False

    def get(self, name: str) -> SourceState | None:
        """Return the state for ``name``, or ``None`` if it is unknown."""
        return self.sources.get(name)

    def set(self, name: str, state: SourceState) -> None:
        """Replace the state for ``name``."""
        self.sources[name] = state

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "version": STATE_VERSION,
            "sources": {name: state.to_dict() for name, state in sorted(self.sources.items())},
        }


def load_state(path: str | Path) -> PipelineState:
    """Read the state file, returning empty state when it is absent or invalid."""
    state_path = Path(path)
    if not state_path.exists():
        return PipelineState(fresh=True)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PipelineState(fresh=True)
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return PipelineState(fresh=True)
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return PipelineState(fresh=True)
    return PipelineState(
        sources={name: SourceState.from_dict(entry) for name, entry in sources.items()}
    )


def save_state(path: str | Path, state: PipelineState, *, fsync: bool = True) -> Path:
    """Write the state file atomically."""
    return atomic_write_json(path, state.to_dict(), indent=None, fsync=fsync)
