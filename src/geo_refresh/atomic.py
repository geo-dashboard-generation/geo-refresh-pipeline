"""Atomic file writes.

A scheduled pipeline writes artifacts that a live dashboard reads. If the
process dies mid-write, the dashboard must not see a truncated GeoJSON file, so
every write goes to a temporary file in the *same directory* (guaranteeing the
rename stays on one filesystem) and is then renamed over the target.
``os.replace`` is atomic on POSIX and on Windows.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import OutputError


def atomic_write_bytes(path: str | Path, data: bytes, *, fsync: bool = True) -> Path:
    """Write ``data`` to ``path`` atomically, creating parent directories.

    Args:
        path: Destination file.
        data: Bytes to write.
        fsync: Flush to disk before the rename. Correct but slower; tests turn
            it off.

    Returns:
        The resolved destination path.

    Raises:
        OutputError: If the write or rename fails. The temporary file is
            removed, so a failure never leaves a partial artifact *or* stray
            temp files behind.
    """
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"cannot create directory {destination.parent}: {exc}") from exc

    handle = None
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            dir=str(destination.parent),
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        handle = os.fdopen(fd, "wb")
        handle.write(data)
        if fsync:
            handle.flush()
            os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temp_name, destination)
        temp_name = None
    except OSError as exc:
        raise OutputError(f"cannot write {destination}: {exc}") from exc
    finally:
        if handle is not None:
            handle.close()
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)
    return destination


def atomic_write_text(
    path: str | Path, text: str, *, encoding: str = "utf-8", fsync: bool = True
) -> Path:
    """Write ``text`` to ``path`` atomically."""
    return atomic_write_bytes(path, text.encode(encoding), fsync=fsync)


def atomic_write_json(
    path: str | Path, payload: Any, *, indent: int | None = 2, fsync: bool = True
) -> Path:
    """Serialise ``payload`` to JSON and write it atomically.

    Serialisation happens *before* the file is opened, so an unserialisable
    payload fails without touching the destination at all.
    """
    try:
        text = json.dumps(payload, indent=indent, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OutputError(f"cannot serialise JSON for {path}: {exc}") from exc
    if indent is not None:
        text += "\n"
    return atomic_write_text(path, text, fsync=fsync)
