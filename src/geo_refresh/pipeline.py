"""The run orchestrator: fetch, parse, transform, diff, write, build.

One run does this per source:

1. **Fetch** the payload, with retries and a timeout.
2. **Parse** it into GeoJSON features according to ``format`` and ``mapping``.
3. **Transform** it through the configured steps.
4. **Validate** the result against ``min_features``.
5. **Hash** the feature set and compare it with the hash stored by the previous
   run to decide whether anything actually changed.
6. **Diff** it against the previous id->hash index for the add/remove/modify
   counts.
7. **Write** that source's outputs — but only if it changed, or ``--force``.

Then, once every source is done, pipeline-level outputs run over the merged
feature set, the manifest is written, and the run state is saved.

Two safety rules are worth stating up front, because they decide what a partly
broken upstream does to a live dashboard:

* If **any** source fails, pipeline-level outputs and build commands are
  skipped. Publishing half a dataset is worse than publishing yesterday's.
* The manifest is written **anyway**, so the stale-data badge can react to the
  failure even though no artifacts were regenerated.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from .config import GeoJsonOutput, PipelineConfig, Source, SummaryOutput
from .diffing import build_index, compute_diff
from .errors import (
    EXIT_NO_CHANGE,
    EXIT_OK,
    EXIT_VALIDATION_FAILURE,
    GeoRefreshError,
    ValidationError,
)
from .formats import parse_payload, records_to_features
from .geometry import bounding_box
from .hashing import collection_hash, hashes_equal
from .logging import Logger
from .manifest import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_UNCHANGED,
    Manifest,
    SourceManifest,
    compute_stale,
    iso8601,
    utcnow,
)
from .outputs import apply_outputs
from .sources import ClientFactory, FetchResult, fetch
from .state import PipelineState, SourceState, load_state, save_state

Feature = dict[str, Any]


@dataclass
class RunOptions:
    """Everything that changes how a run behaves but is not in the config file."""

    dry_run: bool = False
    force: bool = False
    only: list[str] = field(default_factory=list)
    skip_build: bool = False
    fsync: bool = True
    #: Injected in tests to avoid real network / clock / randomness.
    client_factory: ClientFactory | None = None
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random | None = None
    now: datetime | None = None


@dataclass
class RunResult:
    """The outcome of one pipeline run."""

    manifest: Manifest
    manifest_path: str | None
    exit_code: int
    features: dict[str, list[Feature]] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Whether any source's content changed."""
        return self.manifest.changed

    @property
    def stale(self) -> bool:
        """Whether any source is past its ``max_age``."""
        return self.manifest.stale

    @property
    def ok(self) -> bool:
        """Whether the run completed without a failure (no-change counts as ok)."""
        return self.exit_code in (EXIT_OK, EXIT_NO_CHANGE)


def select_sources(config: PipelineConfig, only: Sequence[str]) -> list[Source]:
    """Return the sources to run, honouring ``--only``.

    Raises:
        ValidationError: If a requested name does not exist, listing the ones
            that do.
    """
    if not only:
        return list(config.sources)
    selected: list[Source] = []
    known = [source.name for source in config.sources]
    for name in only:
        source = config.source_by_name(name)
        if source is None:
            raise ValidationError(
                f"unknown source {name!r}; this config defines: {', '.join(known)}"
            )
        if source not in selected:
            selected.append(source)
    return selected


def _features_from_fetch(source: Source, result: FetchResult) -> list[Feature]:
    if result.records is not None:
        if source.mapping is None:  # pragma: no cover - config validation guarantees it
            raise ValidationError("record-based source needs a mapping", source=source.name)
        return records_to_features(result.records, source.mapping)
    encoding = getattr(source, "encoding", "utf-8")
    return parse_payload(result.payload, source.format, source.mapping, encoding=encoding)


def _carry_forward(
    source: Source,
    config: PipelineConfig,
    state: PipelineState,
    status: str,
    *,
    error: str | None = None,
    error_code: int | None = None,
    now: datetime | None = None,
) -> SourceManifest:
    """Build a manifest entry for a source that did not produce fresh data.

    The previous ``fetched_at`` is carried forward so the staleness computation
    keeps working: a source that has been failing for a day looks a day old.
    """
    previous = state.get(source.name)
    max_age = config.effective(source, "max_age")
    entry = SourceManifest(
        name=source.name,
        status=status,
        format=source.format,
        fetched_at=previous.fetched_at if previous else None,
        content_hash=previous.content_hash if previous else None,
        previous_content_hash=previous.content_hash if previous else None,
        feature_count=previous.feature_count if previous else 0,
        changed=False,
        max_age_seconds=max_age,
        error=error,
        error_code=error_code,
    )
    entry.stale = compute_stale(entry.fetched_at, max_age, now=now)
    return entry


def run_source(
    source: Source,
    config: PipelineConfig,
    state: PipelineState,
    options: RunOptions,
    logger: Logger,
) -> tuple[SourceManifest, list[Feature], SourceState | None]:
    """Fetch, parse, transform, hash and diff a single source.

    Returns:
        The manifest entry, the resulting features, and the new state to
        persist (``None`` when the source failed and its state must be kept).
    """
    from .transforms import apply_transforms

    now = options.now or utcnow()
    previous = state.get(source.name)
    max_age = config.effective(source, "max_age")

    try:
        result = fetch(
            source,
            config,
            logger=logger,
            client_factory=options.client_factory,
            sleep=options.sleep,
            rng=options.rng,
        )
        features = _features_from_fetch(source, result)
        features, reports = apply_transforms(features, source.transform)
        minimum = int(config.effective(source, "min_features"))
        if len(features) < minimum:
            raise ValidationError(
                f"only {len(features)} feature(s) survived the pipeline but "
                f"min_features is {minimum}. Refusing to publish what looks like a "
                f"truncated upstream response.",
                source=source.name,
            )
        content_hash = collection_hash(features)
    except GeoRefreshError as error:
        logger.error("source.failed", source=source.name, error=str(error))
        entry = _carry_forward(
            source,
            config,
            state,
            STATUS_FAILED,
            error=str(error),
            error_code=error.exit_code,
            now=now,
        )
        return entry, [], None

    # `options.now` pins the run's reference clock (tests, reproducible runs);
    # otherwise the real fetch time is used.
    fetched_at = options.now or result.fetched_at
    previous_hash = previous.content_hash if previous else None
    changed = not hashes_equal(previous_hash, content_hash)
    diff = compute_diff(
        features,
        previous.index if previous and previous.content_hash else None,
        id_property=source.id_property,
    )
    index, _ = build_index(features, source.id_property)

    entry = SourceManifest(
        name=source.name,
        status=STATUS_OK if changed else STATUS_UNCHANGED,
        origin=result.origin,
        format=source.format,
        fetched_at=iso8601(fetched_at),
        last_modified=result.last_modified,
        etag=result.etag,
        feature_count=len(features),
        content_hash=content_hash,
        previous_content_hash=previous_hash,
        changed=changed,
        max_age_seconds=max_age,
        stale=compute_stale(fetched_at, max_age, now=now),
        bbox=bounding_box(features),
        attempts=result.attempts,
        duration_ms=int(result.duration * 1000),
        bytes_read=result.bytes_read,
        diff=diff,
        transforms=[report.to_dict() for report in reports],
    )
    logger.info(
        "source.done",
        source=source.name,
        features=len(features),
        changed=changed,
        diff=diff.describe(),
        attempts=result.attempts,
    )
    new_state = SourceState(
        content_hash=content_hash,
        index=index,
        fetched_at=entry.fetched_at,
        feature_count=len(features),
    )
    return entry, features, new_state


def run_pipeline(
    config: PipelineConfig,
    options: RunOptions | None = None,
    logger: Logger | None = None,
) -> RunResult:
    """Execute a whole pipeline.

    Args:
        config: A validated pipeline configuration.
        options: Run-time switches (``--dry-run``, ``--only``, ``--force``, …).
        logger: Where to send progress. A silent logger is used if omitted.

    Returns:
        A :class:`RunResult` whose ``exit_code`` follows the documented
        contract: 0 changed, 3 nothing changed, 4 a fetch failed, 5 validation
        failed, 7 an output failed.
    """
    options = options or RunOptions()
    logger = logger or Logger(level="silent")
    now = options.now or utcnow()

    state_path = config.resolve(config.state)
    manifest_path = config.resolve(config.manifest)
    state = load_state(state_path)

    selected = select_sources(config, options.only)
    selected_names = {source.name for source in selected}
    partial = bool(options.only) and len(selected) < len(config.sources)

    manifest = Manifest(generated_at=now, dry_run=options.dry_run)
    features_by_source: dict[str, list[Feature]] = {}
    new_states: dict[str, SourceState] = {}
    performed: list[str] = []

    for source in config.sources:
        if source.name not in selected_names:
            manifest.sources.append(
                _carry_forward(source, config, state, STATUS_SKIPPED, now=now)
            )
            continue
        entry, features, new_state = run_source(source, config, state, options, logger)
        manifest.sources.append(entry)
        if new_state is not None:
            new_states[source.name] = new_state
            features_by_source[source.name] = features

    failures = manifest.failed
    changed_sources = [s for s in manifest.sources if s.changed]
    should_write = bool(changed_sources) or options.force

    if failures:
        logger.error(
            "run.blocked",
            failed=",".join(s.name for s in failures),
            reason="outputs skipped because at least one source failed",
        )
    elif not should_write:
        logger.info("run.no-change", sources=len(selected))

    if not failures and should_write:
        for source in selected:
            if source.name not in features_by_source:
                continue
            entry = manifest.get(source.name)
            if entry is not None and not entry.changed and not options.force:
                continue
            outputs = [
                output
                for output in source.outputs
                if not (options.skip_build and getattr(output, "type", "") == "build")
            ]
            written = apply_outputs(
                outputs,
                features_by_source[source.name],
                config,
                name=source.name,
                logger=logger,
                dry_run=options.dry_run,
                fsync=options.fsync,
            )
            if entry is not None:
                entry.outputs = written
            performed.extend(written)

        merged: list[Feature] = []
        for source in config.sources:
            merged.extend(features_by_source.get(source.name, []))
        global_outputs = [
            output
            for output in config.outputs
            if not (options.skip_build and getattr(output, "type", "") == "build")
        ]
        if partial:
            file_outputs = [
                o for o in global_outputs if isinstance(o, (GeoJsonOutput, SummaryOutput))
            ]
            if file_outputs:
                logger.warning(
                    "run.partial",
                    reason=(
                        "pipeline-level file outputs skipped because --only ran a "
                        "subset of sources; they would contain incomplete data"
                    ),
                )
            global_outputs = [o for o in global_outputs if o not in file_outputs]
        performed.extend(
            apply_outputs(
                global_outputs,
                merged,
                config,
                logger=logger,
                dry_run=options.dry_run,
                fsync=options.fsync,
            )
        )

    manifest.outputs = performed

    if not options.dry_run:
        from .atomic import atomic_write_json

        atomic_write_json(manifest_path, manifest.to_dict(now), fsync=options.fsync)
        for name, new_state in new_states.items():
            state.set(name, new_state)
        save_state(state_path, state, fsync=options.fsync)
        logger.info("manifest.written", path=str(manifest_path), changed=manifest.changed)

    exit_code = EXIT_OK
    if failures:
        exit_code = max(
            entry.error_code or EXIT_VALIDATION_FAILURE for entry in failures
        )
    elif not changed_sources and not options.force:
        exit_code = EXIT_NO_CHANGE

    return RunResult(
        manifest=manifest,
        manifest_path=None if options.dry_run else str(manifest_path),
        exit_code=exit_code,
        features=features_by_source,
        outputs=performed,
    )
