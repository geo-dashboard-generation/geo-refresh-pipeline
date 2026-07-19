"""Manifest fields and staleness, atomic writes, run state, retry/backoff."""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from geo_refresh.atomic import atomic_write_bytes, atomic_write_json, atomic_write_text
from geo_refresh.diffing import DiffSummary
from geo_refresh.errors import OutputError
from geo_refresh.manifest import (
    MANIFEST_SCHEMA,
    Manifest,
    SourceManifest,
    compute_stale,
    humanize_age,
    iso8601,
    load_manifest_document,
    parse_iso8601,
)
from geo_refresh.retry import RetryPolicy, call_with_retries
from geo_refresh.state import PipelineState, SourceState, load_state, save_state

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


# -- timestamps and staleness ---------------------------------------------- #


def test_iso8601_round_trip() -> None:
    assert iso8601(NOW) == "2026-07-19T12:00:00Z"
    assert parse_iso8601("2026-07-19T12:00:00Z") == NOW


def test_parse_iso8601_assumes_utc_when_naive() -> None:
    assert parse_iso8601("2026-07-19T12:00:00") == NOW


def test_parse_iso8601_rejects_garbage() -> None:
    with pytest.raises(Exception, match="ISO 8601"):
        parse_iso8601("yesterday")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "just now"),
        (44, "just now"),
        (60, "1 minute ago"),
        (600, "10 minutes ago"),
        (3600, "1 hour ago"),
        (10800, "3 hours ago"),
        (86400, "1 day ago"),
        (604800, "1 week ago"),
        (2592000, "1 month ago"),
        (31536000, "1 year ago"),
        (-5, "in the future"),
    ],
)
def test_humanize_age(seconds: float, expected: str) -> None:
    assert humanize_age(seconds) == expected


def test_stale_is_false_without_a_max_age() -> None:
    assert compute_stale(NOW - timedelta(days=365), None, now=NOW) is False


def test_stale_flips_once_max_age_is_exceeded() -> None:
    assert compute_stale(NOW - timedelta(hours=5), 21600, now=NOW) is False
    assert compute_stale(NOW - timedelta(hours=7), 21600, now=NOW) is True


def test_never_fetched_data_is_stale_when_a_max_age_is_set() -> None:
    assert compute_stale(None, 60, now=NOW) is True


def test_stale_accepts_an_iso_string() -> None:
    assert compute_stale("2026-07-18T12:00:00Z", 3600, now=NOW) is True


# -- manifest --------------------------------------------------------------- #


def build_manifest() -> Manifest:
    fresh = SourceManifest(
        name="fresh",
        fetched_at=iso8601(NOW - timedelta(minutes=5)),
        last_modified="Sun, 19 Jul 2026 11:00:00 GMT",
        etag='W/"abc123"',
        feature_count=20,
        content_hash="sha256-features-v1:aaa",
        previous_content_hash="sha256-features-v1:bbb",
        changed=True,
        max_age_seconds=21600,
        diff=DiffSummary(added=1, removed=0, modified=2, unchanged=17, total=20, keyed=True),
    )
    old = SourceManifest(
        name="old",
        status="failed",
        fetched_at=iso8601(NOW - timedelta(days=2)),
        feature_count=5,
        max_age_seconds=3600,
        stale=True,
        error="giving up after 4 attempts",
        error_code=4,
    )
    return Manifest(generated_at=NOW, sources=[fresh, old])


def test_manifest_document_has_every_documented_field() -> None:
    payload = build_manifest().to_dict(NOW)
    assert payload["schema"] == MANIFEST_SCHEMA
    assert payload["generated_at"] == "2026-07-19T12:00:00Z"
    assert payload["changed"] is True
    assert payload["stale"] is True
    assert payload["totals"] == {
        "sources": 2,
        "features": 25,
        "changed_sources": 1,
        "stale_sources": 1,
        "failed_sources": 1,
    }
    entry = payload["sources"]["fresh"]
    for key in (
        "status",
        "fetched_at",
        "age_seconds",
        "last_modified",
        "etag",
        "feature_count",
        "content_hash",
        "previous_content_hash",
        "changed",
        "max_age_seconds",
        "stale",
        "diff",
    ):
        assert key in entry
    assert entry["age_seconds"] == 300
    assert entry["diff"]["modified"] == 2


def test_manifest_reports_failures_and_lookup() -> None:
    manifest = build_manifest()
    assert [entry.name for entry in manifest.failed] == ["old"]
    assert manifest.get("fresh") is not None
    assert manifest.get("nope") is None
    assert manifest.feature_count == 25


def test_age_seconds_is_none_when_never_fetched() -> None:
    assert SourceManifest(name="x").age_seconds(NOW) is None


def test_load_manifest_document(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(build_manifest().to_dict(NOW)), encoding="utf-8")
    assert load_manifest_document(path)["schema"] == MANIFEST_SCHEMA


def test_load_manifest_document_errors(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="no manifest at"):
        load_manifest_document(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(Exception, match="not valid JSON"):
        load_manifest_document(bad)
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(Exception, match="does not look like"):
        load_manifest_document(wrong)


# -- atomic writes ---------------------------------------------------------- #


def test_atomic_write_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "out.txt"
    atomic_write_text(target, "hello", fsync=False)
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    atomic_write_bytes(tmp_path / "a.bin", b"x" * 100, fsync=False)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.bin"]


def test_atomic_write_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    atomic_write_json(target, {"v": 1}, fsync=False)
    atomic_write_json(target, {"v": 2}, fsync=False)
    assert json.loads(target.read_text(encoding="utf-8"))["v"] == 2


def test_unserialisable_payload_never_touches_the_destination(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(OutputError, match="cannot serialise JSON"):
        atomic_write_json(target, {"bad": object()}, fsync=False)
    assert target.read_text(encoding="utf-8") == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.json"]


def test_write_failure_cleans_up_and_preserves_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out.json"
    target.write_text("original", encoding="utf-8")
    real_replace = os.replace

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OutputError, match="disk full"):
        atomic_write_json(target, {"v": 2}, fsync=False)
    monkeypatch.setattr(os, "replace", real_replace)
    assert target.read_text(encoding="utf-8") == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.json"]


def test_write_to_an_unwritable_location_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OutputError):
        atomic_write_text(blocker / "child.txt", "x", fsync=False)


# -- state ------------------------------------------------------------------ #


def test_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = PipelineState()
    state.set("a", SourceState(content_hash="h", index={"1": "x"}, fetched_at="t", feature_count=1))
    save_state(path, state, fsync=False)
    reloaded = load_state(path)
    assert reloaded.fresh is False
    entry = reloaded.get("a")
    assert entry is not None and entry.content_hash == "h" and entry.feature_count == 1


def test_missing_state_file_is_a_first_run(tmp_path: Path) -> None:
    assert load_state(tmp_path / "absent.json").fresh is True


def test_corrupt_state_file_degrades_to_a_first_run(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_state(path).fresh is True


def test_state_from_a_future_version_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 99, "sources": {}}), encoding="utf-8")
    assert load_state(path).fresh is True


# -- retry ------------------------------------------------------------------ #


def test_retry_policy_delays_grow_exponentially_without_jitter() -> None:
    policy = RetryPolicy(attempts=5, backoff=1.0, max_backoff=100.0, jitter=False)
    assert list(policy.delays()) == [1.0, 2.0, 4.0, 8.0]


def test_retry_policy_caps_at_max_backoff() -> None:
    policy = RetryPolicy(attempts=8, backoff=1.0, max_backoff=5.0, jitter=False)
    assert max(policy.delays()) == 5.0


def test_retry_policy_jitter_stays_inside_the_window() -> None:
    policy = RetryPolicy(attempts=6, backoff=1.0, max_backoff=10.0)
    rng = random.Random(1234)
    for attempt in range(2, 7):
        window = min(10.0, 1.0 * (2 ** (attempt - 2)))
        assert 0.0 <= policy.delay_for(attempt, rng) <= window


def test_retry_policy_from_retries() -> None:
    assert RetryPolicy.from_retries(3).attempts == 4
    assert RetryPolicy.from_retries(0).attempts == 1


def test_retry_policy_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RetryPolicy(attempts=0)
    with pytest.raises(ValueError, match="must not be negative"):
        RetryPolicy(backoff=-1)


def test_call_with_retries_succeeds_after_transient_failures() -> None:
    slept: list[float] = []
    attempts: list[int] = []

    def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt < 3:
            raise RuntimeError("boom")
        return "ok"

    result = call_with_retries(
        operation,
        RetryPolicy(attempts=4, backoff=1.0, jitter=False),
        sleep=slept.append,
    )
    assert result == "ok"
    assert attempts == [1, 2, 3]
    assert slept == [1.0, 2.0]


def test_call_with_retries_raises_the_last_error() -> None:
    def operation(_attempt: int) -> str:
        raise RuntimeError("always down")

    with pytest.raises(RuntimeError, match="always down"):
        call_with_retries(
            operation, RetryPolicy(attempts=3, backoff=0.0), sleep=lambda _s: None
        )


def test_call_with_retries_can_stop_early() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> str:
        attempts.append(attempt)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        call_with_retries(
            operation,
            RetryPolicy(attempts=5, backoff=0.0),
            should_retry=lambda _error: False,
            sleep=lambda _s: None,
        )
    assert attempts == [1]


def test_call_with_retries_reports_each_retry() -> None:
    events: list[tuple[int, float]] = []

    def operation(attempt: int) -> str:
        if attempt < 3:
            raise RuntimeError("boom")
        return "ok"

    call_with_retries(
        operation,
        RetryPolicy(attempts=3, backoff=1.0, jitter=False),
        on_retry=lambda attempt, delay, _error: events.append((attempt, delay)),
        sleep=lambda _s: None,
    )
    assert events == [(2, 1.0), (3, 2.0)]
