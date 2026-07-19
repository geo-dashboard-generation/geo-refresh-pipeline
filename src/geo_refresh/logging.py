"""Two log formats: readable text for humans, one JSON object per line for CI.

Log records go to stderr so that stdout stays clean for machine-readable
command output (``status --json`` pipes into ``jq`` without filtering).
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

LogFormat = Literal["text", "json"]

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "silent": 100}

_COLOURS = {
    "debug": "\033[2m",
    "info": "\033[0m",
    "warning": "\033[33m",
    "error": "\033[31m",
}
_RESET = "\033[0m"


@dataclass
class Logger:
    """A minimal structured logger.

    Attributes:
        fmt: ``"text"`` or ``"json"``.
        level: Minimum level to emit; ``"silent"`` suppresses everything.
        stream: Destination, defaulting to stderr.
        colour: Force ANSI colour on/off; ``None`` auto-detects a TTY.
    """

    fmt: LogFormat = "text"
    level: str = "info"
    stream: TextIO = field(default_factory=lambda: sys.stderr)
    colour: bool | None = None

    def __post_init__(self) -> None:
        if self.level not in _LEVELS:
            raise ValueError(
                f"unknown log level {self.level!r}; expected one of {', '.join(_LEVELS)}"
            )

    def _enabled(self, level: str) -> bool:
        return _LEVELS[level] >= _LEVELS[self.level]

    def _use_colour(self) -> bool:
        if self.colour is not None:
            return self.colour
        return bool(getattr(self.stream, "isatty", lambda: False)())

    def log(self, level: str, event: str, **fields: Any) -> None:
        """Emit one record."""
        if not self._enabled(level):
            return
        if self.fmt == "json":
            payload = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "level": level,
                "event": event,
                **fields,
            }
            self.stream.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
        else:
            extra = " ".join(f"{k}={_render(v)}" for k, v in fields.items())
            line = f"{event}{' ' + extra if extra else ''}"
            if self._use_colour():
                line = f"{_COLOURS.get(level, '')}{line}{_RESET}"
            self.stream.write(line + "\n")
        self.stream.flush()

    def debug(self, event: str, **fields: Any) -> None:
        """Emit a debug record."""
        self.log("debug", event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        """Emit an info record."""
        self.log("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        """Emit a warning record."""
        self.log("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        """Emit an error record."""
        self.log("error", event, **fields)


def _render(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    text = str(value)
    return f'"{text}"' if " " in text else text
