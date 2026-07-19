"""Fetching raw data from the three supported source types.

Each fetcher returns a :class:`FetchResult` carrying the payload plus the
upstream freshness metadata (``Last-Modified``, ``ETag``) that ends up in the
manifest. Retries, timeouts and backoff are applied here rather than in the
pipeline so that every source type behaves the same way.

The ``sql`` source imports SQLAlchemy lazily: the rest of the tool works fine
without it, and a config that *does* use a sql source gets a clear install
message instead of an ``ImportError`` traceback.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .config import FileSource, HttpSource, PipelineConfig, Source, SqlSource
from .envsubst import expand, expand_tree
from .errors import FetchError, MissingDependencyError
from .logging import Logger
from .retry import RetryPolicy, call_with_retries

#: HTTP status codes worth retrying: transient server and rate-limit errors.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})


@dataclass
class FetchResult:
    """Raw payload plus upstream freshness metadata."""

    origin: str
    payload: bytes = b""
    records: list[dict[str, Any]] | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_modified: str | None = None
    etag: str | None = None
    status: int | None = None
    content_type: str | None = None
    attempts: int = 1
    duration: float = 0.0

    @property
    def bytes_read(self) -> int:
        """Size of the payload in bytes (0 for record-based sources)."""
        return len(self.payload)


class ClientFactory(Protocol):
    """Callable returning an ``httpx.Client``. Injected by the tests."""

    def __call__(self, *, timeout: float, verify: bool) -> Any: ...  # pragma: no cover


def _default_client_factory(*, timeout: float, verify: bool) -> Any:
    import httpx

    return httpx.Client(
        timeout=timeout,
        verify=verify,
        follow_redirects=True,
        headers={"User-Agent": "geo-refresh-pipeline"},
    )


class _RetryableHttpError(Exception):
    """Internal marker for an HTTP failure that is worth another attempt."""


def _policy_for(config: PipelineConfig, source: Source) -> RetryPolicy:
    return RetryPolicy.from_retries(
        retries=int(config.effective(source, "retries")),
        backoff=float(config.effective(source, "retry_backoff")),
        max_backoff=float(config.defaults.retry_max_backoff),
    )


def fetch_http(
    source: HttpSource,
    config: PipelineConfig,
    *,
    logger: Logger | None = None,
    client_factory: ClientFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> FetchResult:
    """Fetch an HTTP source, retrying transient failures.

    ``${ENV}`` references in the URL, headers, params and body are expanded
    immediately before the request, so secrets never live in the config file.

    Raises:
        FetchError: On a non-retryable response or after the retry budget.
    """
    import httpx

    url = expand(source.url, where=f"source '{source.name}' url")
    headers = expand_tree(source.headers, where=f"source '{source.name}' headers")
    params = expand_tree(source.params, where=f"source '{source.name}' params")
    body = (
        expand(source.body, where=f"source '{source.name}' body")
        if source.body is not None
        else None
    )
    timeout = float(config.effective(source, "timeout"))
    policy = _policy_for(config, source)
    factory = client_factory or _default_client_factory
    started = time.monotonic()
    attempts_used = 0

    def attempt(attempt_number: int) -> Any:
        nonlocal attempts_used
        attempts_used = attempt_number
        client = factory(timeout=timeout, verify=source.verify_tls)
        try:
            response = client.request(
                source.method,
                url,
                headers=headers,
                params=params or None,
                content=body.encode("utf-8") if body is not None else None,
            )
        except httpx.TimeoutException as exc:
            raise _RetryableHttpError(f"timed out after {timeout:g}s: {exc}") from exc
        except httpx.TransportError as exc:
            raise _RetryableHttpError(f"transport error: {exc}") from exc
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        if response.status_code in RETRYABLE_STATUS:
            raise _RetryableHttpError(
                f"HTTP {response.status_code} {response.reason_phrase}"
            )
        if response.status_code >= 400:
            raise FetchError(
                f"HTTP {response.status_code} {response.reason_phrase} for {url}. "
                f"This status is not retried because it will not succeed on a retry; "
                f"check the URL and any auth headers.",
                source=source.name,
            )
        return response

    def on_retry(next_attempt: int, delay: float, error: BaseException) -> None:
        if logger:
            logger.warning(
                "fetch.retry",
                source=source.name,
                attempt=next_attempt,
                delay=round(delay, 3),
                reason=str(error),
            )

    try:
        response = call_with_retries(
            attempt,
            policy,
            retry_on=(_RetryableHttpError,),
            on_retry=on_retry,
            sleep=sleep,
            rng=rng,
        )
    except _RetryableHttpError as exc:
        raise FetchError(
            f"giving up on {url} after {policy.attempts} attempt(s): {exc}",
            source=source.name,
        ) from exc

    return FetchResult(
        origin=url,
        payload=response.content,
        status=response.status_code,
        last_modified=response.headers.get("last-modified"),
        etag=response.headers.get("etag"),
        content_type=response.headers.get("content-type"),
        attempts=attempts_used,
        duration=time.monotonic() - started,
    )


def fetch_file(
    source: FileSource, config: PipelineConfig, *, logger: Logger | None = None
) -> FetchResult:
    """Read a local file, using its mtime as the upstream Last-Modified.

    Raises:
        FetchError: If the file is missing or unreadable.
    """
    path = config.resolve(expand(source.path, where=f"source '{source.name}' path"))
    started = time.monotonic()
    try:
        payload = path.read_bytes()
        stat = path.stat()
    except FileNotFoundError as exc:
        raise FetchError(
            f"file not found: {path}. Paths are resolved relative to the config file "
            f"({config.base_dir}).",
            source=source.name,
        ) from exc
    except IsADirectoryError as exc:
        raise FetchError(f"{path} is a directory, not a file", source=source.name) from exc
    except OSError as exc:
        raise FetchError(f"cannot read {path}: {exc}", source=source.name) from exc
    if logger:
        logger.debug("fetch.file", source=source.name, path=str(path), bytes=len(payload))
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return FetchResult(
        origin=str(path),
        payload=payload,
        last_modified=modified.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        attempts=1,
        duration=time.monotonic() - started,
    )


def fetch_sql(
    source: SqlSource, config: PipelineConfig, *, logger: Logger | None = None
) -> FetchResult:
    """Run a query and return the rows as records.

    Raises:
        MissingDependencyError: If SQLAlchemy is not installed.
        FetchError: If the connection or query fails.
    """
    try:
        import sqlalchemy
    except ImportError as exc:
        raise MissingDependencyError(
            "the 'sql' source type needs SQLAlchemy, which is not installed. "
            "Install it with:  .venv/bin/python -m pip install 'SQLAlchemy>=2.0'  "
            "(or use the optional extra: uv pip install -e '.[sql]'). Every other "
            "source type works without it.",
            source=source.name,
        ) from exc

    url = expand(source.url, where=f"source '{source.name}' url")
    started = time.monotonic()
    try:
        engine = sqlalchemy.create_engine(url)
    except Exception as exc:  # SQLAlchemy raises a wide variety here
        raise FetchError(
            f"cannot create a database engine for the configured url: {exc}",
            source=source.name,
        ) from exc
    try:
        with engine.connect() as connection:
            result = connection.execute(sqlalchemy.text(source.query))
            columns = list(result.keys())
            records = [dict(zip(columns, row)) for row in result.fetchall()]
    except Exception as exc:
        raise FetchError(
            f"query failed: {exc}. Query was: {source.query.strip()[:200]}",
            source=source.name,
        ) from exc
    finally:
        engine.dispose()

    if logger:
        logger.debug("fetch.sql", source=source.name, rows=len(records))
    return FetchResult(
        origin=_redact_url(url),
        records=records,
        attempts=1,
        duration=time.monotonic() - started,
    )


def _redact_url(url: str) -> str:
    """Strip credentials from a database URL before it reaches a log or manifest."""
    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if "@" in rest:
        _, _, host = rest.rpartition("@")
        return f"{scheme}://***@{host}"
    return url


def fetch(
    source: Source,
    config: PipelineConfig,
    *,
    logger: Logger | None = None,
    client_factory: ClientFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> FetchResult:
    """Fetch any source type, dispatching on the discriminator.

    Raises:
        FetchError: On any unrecoverable read failure.
    """
    if isinstance(source, HttpSource):
        return fetch_http(
            source,
            config,
            logger=logger,
            client_factory=client_factory,
            sleep=sleep,
            rng=rng,
        )
    if isinstance(source, FileSource):
        return fetch_file(source, config, logger=logger)
    if isinstance(source, SqlSource):
        return fetch_sql(source, config, logger=logger)
    raise FetchError(  # pragma: no cover - discriminated union is exhaustive
        f"unsupported source type: {type(source).__name__}", source=getattr(source, "name", None)
    )
