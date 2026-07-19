"""Error types and process exit codes used across the pipeline.

Exit codes are part of the CLI contract: CI jobs and the bundled GitHub Action
branch on them, so they must stay stable.
"""

from __future__ import annotations

# Exit codes. 0 means "work happened and everything succeeded".
EXIT_OK = 0
EXIT_NO_CHANGE = 3
EXIT_FETCH_FAILURE = 4
EXIT_VALIDATION_FAILURE = 5
EXIT_CONFIG_ERROR = 6
EXIT_OUTPUT_FAILURE = 7

EXIT_CODE_NAMES: dict[int, str] = {
    EXIT_OK: "ok",
    EXIT_NO_CHANGE: "no-change",
    EXIT_FETCH_FAILURE: "fetch-failure",
    EXIT_VALIDATION_FAILURE: "validation-failure",
    EXIT_CONFIG_ERROR: "config-error",
    EXIT_OUTPUT_FAILURE: "output-failure",
}


class GeoRefreshError(Exception):
    """Base class for every error the pipeline raises deliberately.

    Carries the process exit code that the CLI should use when the error
    escapes to the top level.
    """

    exit_code: int = EXIT_VALIDATION_FAILURE

    def __init__(self, message: str, *, source: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.source = source

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.source:
            return f"[source: {self.source}] {self.message}"
        return self.message


class ConfigError(GeoRefreshError):
    """The pipeline configuration file is missing, unreadable or invalid."""

    exit_code = EXIT_CONFIG_ERROR


class FetchError(GeoRefreshError):
    """A source could not be read after exhausting the retry budget."""

    exit_code = EXIT_FETCH_FAILURE


class ValidationError(GeoRefreshError):
    """Fetched payload could not be parsed or failed a validation rule."""

    exit_code = EXIT_VALIDATION_FAILURE


class OutputError(GeoRefreshError):
    """An output step (file write, build command) failed."""

    exit_code = EXIT_OUTPUT_FAILURE


class MissingDependencyError(GeoRefreshError):
    """An optional third-party dependency is needed but not installed."""

    exit_code = EXIT_CONFIG_ERROR
