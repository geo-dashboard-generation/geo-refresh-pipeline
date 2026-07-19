"""HTTP source fetching. Every request goes through httpx's MockTransport."""

from __future__ import annotations

import httpx
import pytest

from conftest import make_client_factory, sequence_handler
from geo_refresh.config import parse_config
from geo_refresh.envsubst import expand, expand_tree
from geo_refresh.errors import ConfigError, FetchError
from geo_refresh.sources import fetch_http

BODY = '{"type": "FeatureCollection", "features": []}'


def http_config(**source: object):
    payload = {"name": "feed", "type": "http", "url": "https://data.example.org/feed.geojson"}
    payload.update(source)
    return parse_config({"defaults": {"retries": 2, "retry_backoff": 0}, "sources": [payload]})


def fetch(config, handler, **kwargs):
    return fetch_http(
        config.sources[0],
        config,
        client_factory=make_client_factory(handler),
        sleep=lambda _s: None,
        **kwargs,
    )


# -- environment substitution ----------------------------------------------- #


def test_env_substitution_in_headers() -> None:
    assert expand("Bearer ${T}", {"T": "secret"}) == "Bearer secret"
    assert expand("${MISSING:-fallback}", {}) == "fallback"
    assert expand("$$literal", {}) == "$literal"


def test_env_substitution_reports_missing_variables() -> None:
    with pytest.raises(ConfigError, match="ALPHA, BETA"):
        expand("${ALPHA}/${BETA}", {}, where="source 'x' url")


def test_env_substitution_walks_nested_structures() -> None:
    result = expand_tree({"a": ["${X}", {"b": "${X}"}]}, {"X": "1"})
    assert result == {"a": ["1", {"b": "1"}]}


# -- fetching --------------------------------------------------------------- #


def test_successful_fetch_captures_freshness_headers() -> None:
    handler, calls = sequence_handler(
        [
            httpx.Response(
                200,
                content=BODY,
                headers={
                    "Last-Modified": "Sun, 19 Jul 2026 11:00:00 GMT",
                    "ETag": 'W/"abc"',
                    "Content-Type": "application/geo+json",
                },
            )
        ]
    )
    result = fetch(http_config(), handler)
    assert result.status == 200
    assert result.last_modified == "Sun, 19 Jul 2026 11:00:00 GMT"
    assert result.etag == 'W/"abc"'
    assert result.content_type == "application/geo+json"
    assert result.attempts == 1
    assert result.bytes_read == len(BODY)
    assert len(calls) == 1


def test_headers_and_params_are_sent_with_env_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEED_TOKEN", "s3cret")
    handler, calls = sequence_handler([httpx.Response(200, content=BODY)])
    config = http_config(
        headers={"Authorization": "Bearer ${FEED_TOKEN}"}, params={"limit": "10"}
    )
    fetch(config, handler)
    assert calls[0].headers["authorization"] == "Bearer s3cret"
    assert calls[0].url.params["limit"] == "10"


def test_missing_env_variable_fails_the_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT_TOKEN", raising=False)
    handler, _ = sequence_handler([httpx.Response(200, content=BODY)])
    with pytest.raises(ConfigError, match="ABSENT_TOKEN"):
        fetch(http_config(headers={"Authorization": "Bearer ${ABSENT_TOKEN}"}), handler)


def test_post_body_is_sent() -> None:
    handler, calls = sequence_handler([httpx.Response(200, content=BODY)])
    fetch(http_config(method="POST", body='{"q": 1}'), handler)
    assert calls[0].method == "POST"
    assert calls[0].content == b'{"q": 1}'


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 408])
def test_transient_statuses_are_retried_then_succeed(status: int) -> None:
    handler, calls = sequence_handler(
        [httpx.Response(status), httpx.Response(200, content=BODY)]
    )
    result = fetch(http_config(), handler)
    assert result.status == 200
    assert result.attempts == 2
    assert len(calls) == 2


def test_retry_budget_is_exhausted_then_reported() -> None:
    handler, calls = sequence_handler([httpx.Response(503)])
    with pytest.raises(FetchError, match="giving up on .* after 3 attempt"):
        fetch(http_config(), handler)
    assert len(calls) == 3


def test_client_errors_are_not_retried() -> None:
    handler, calls = sequence_handler([httpx.Response(404)])
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch(http_config(), handler)
    assert len(calls) == 1


def test_timeouts_are_retried() -> None:
    handler, calls = sequence_handler(
        [httpx.ConnectTimeout("too slow"), httpx.Response(200, content=BODY)]
    )
    assert fetch(http_config(), handler).attempts == 2
    assert len(calls) == 2


def test_transport_errors_are_retried_then_give_up() -> None:
    handler, calls = sequence_handler([httpx.ConnectError("refused")])
    with pytest.raises(FetchError, match="transport error"):
        fetch(http_config(), handler)
    assert len(calls) == 3


def test_retries_can_be_disabled() -> None:
    config = parse_config(
        {
            "defaults": {"retries": 0},
            "sources": [{"name": "feed", "type": "http", "url": "https://example.org/a"}],
        }
    )
    handler, calls = sequence_handler([httpx.Response(503)])
    with pytest.raises(FetchError, match="after 1 attempt"):
        fetch(config, handler)
    assert len(calls) == 1


def test_retry_events_are_logged() -> None:
    from io import StringIO

    from geo_refresh.logging import Logger

    stream = StringIO()
    logger = Logger(fmt="json", level="debug", stream=stream, colour=False)
    handler, _ = sequence_handler([httpx.Response(503), httpx.Response(200, content=BODY)])
    fetch(http_config(), handler, logger=logger)
    assert '"event": "fetch.retry"' in stream.getvalue()
