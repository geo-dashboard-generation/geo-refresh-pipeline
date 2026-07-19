"""Command line interface.

Four commands:

``run``       execute a pipeline
``validate``  parse and check a config without touching the network
``status``    pretty-print an existing manifest
``init``      write a starter config, sample data and badge assets

Exit codes are documented in :mod:`geo_refresh.errors` and are stable: CI and
the bundled GitHub Action branch on them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

from . import __version__
from .config import PipelineConfig, load_config
from .errors import (
    EXIT_CODE_NAMES,
    EXIT_NO_CHANGE,
    EXIT_OK,
    EXIT_VALIDATION_FAILURE,
    GeoRefreshError,
)
from .expressions import compile_expression
from .logging import Logger
from .manifest import humanize_age, load_manifest_document
from .pipeline import RunOptions, run_pipeline, select_sources
from .scaffold import write_scaffold

PROGRAM = "geo-refresh"

_EPILOG = """\
exit codes:
  0  success, at least one source changed (or --force was used)
  3  no change: every source hashed identically to the previous run
  4  fetch failure: a source could not be read after its retries
  5  validation failure: a payload was malformed or failed a rule
  6  configuration error
  7  output failure: a write or build command failed

examples:
  python -m geo_refresh run pipeline.yml
  python -m geo_refresh run pipeline.yml --only bike_stations --force
  python -m geo_refresh run pipeline.yml --dry-run --log-format json
  python -m geo_refresh status build/manifest.json
"""


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Declarative refresh pipeline for map data: fetch, transform, validate, "
            "detect real changes and emit a freshness manifest."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--log-format",
        choices=("text", "json"),
        default="text",
        help="Log record format on stderr (default: text).",
    )
    common.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error", "silent"),
        default="info",
        help="Minimum log level (default: info).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = subparsers.add_parser(
        "run",
        parents=[common],
        help="Fetch every source, regenerate artifacts when the content changed.",
        description=(
            "Fetch every source, transform and validate it, compare the content hash "
            "with the previous run, and regenerate outputs only when something "
            "actually changed."
        ),
    )
    run.add_argument("config", help="Path to the pipeline YAML file.")
    run.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SOURCE",
        help="Run only this source. Repeat for several.",
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="Write outputs and run build commands even if nothing changed.",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and diff, but write nothing and run no build commands.",
    )
    run.add_argument(
        "--skip-build",
        action="store_true",
        help="Write artifacts but do not run any 'build' output.",
    )
    run.add_argument(
        "--summary",
        metavar="PATH",
        help="Also append a GitHub-Actions-style key=value summary to this file.",
    )

    validate = subparsers.add_parser(
        "validate",
        parents=[common],
        help="Check a config file without fetching anything.",
        description=(
            "Parse the configuration, compile every filter expression and report "
            "what the pipeline would do. Makes no network or database calls."
        ),
    )
    validate.add_argument("config", help="Path to the pipeline YAML file.")

    status = subparsers.add_parser(
        "status",
        parents=[common],
        help="Pretty-print a freshness manifest.",
        description="Read manifest.json and show per-source freshness and changes.",
    )
    status.add_argument(
        "manifest",
        nargs="?",
        default="manifest.json",
        help="Path to manifest.json, or to a config file to read its manifest path.",
    )
    status.add_argument(
        "--json", action="store_true", help="Emit the manifest as JSON on stdout."
    )

    init = subparsers.add_parser(
        "init",
        parents=[common],
        help="Write a starter pipeline, sample data and badge assets.",
        description=(
            "Create a runnable example: a pipeline config, a small real GeoJSON "
            "sample, and the stale-data badge snippet."
        ),
    )
    init.add_argument(
        "directory", nargs="?", default=".", help="Where to write (default: current dir)."
    )
    init.add_argument(
        "--force", action="store_true", help="Overwrite files that already exist."
    )
    return parser


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _write_github_summary(path: str, result: Any) -> None:
    """Append ``key=value`` lines that a GitHub Action step can read."""
    lines = [
        f"changed={'true' if result.changed else 'false'}",
        f"stale={'true' if result.stale else 'false'}",
        f"manifest-path={result.manifest_path or ''}",
        f"feature-count={result.manifest.feature_count}",
        f"exit-code={result.exit_code}",
    ]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def command_run(args: argparse.Namespace, out: TextIO) -> int:
    """Execute the ``run`` command."""
    logger = Logger(fmt=args.log_format, level=args.log_level)
    config = load_config(args.config)
    options = RunOptions(
        dry_run=args.dry_run,
        force=args.force,
        only=list(args.only),
        skip_build=args.skip_build,
    )
    result = run_pipeline(config, options, logger)

    if args.summary:
        _write_github_summary(args.summary, result)

    for entry in result.manifest.sources:
        if entry.status == "skipped":
            continue
        marker = {"ok": "changed", "unchanged": "unchanged", "failed": "FAILED"}.get(
            entry.status, entry.status
        )
        detail = entry.diff.describe() if entry.diff else (entry.error or "")
        out.write(f"{entry.name:<24} {marker:<10} {detail}\n")
    if result.outputs:
        out.write(f"\n{len(result.outputs)} output(s):\n")
        for item in result.outputs:
            out.write(f"  {item}\n")
    if result.exit_code == EXIT_NO_CHANGE:
        out.write("\nNothing changed upstream; outputs left untouched.\n")
    elif result.exit_code != EXIT_OK:
        out.write(f"\nRun failed: {EXIT_CODE_NAMES.get(result.exit_code, 'error')}\n")
    return result.exit_code


def describe_config(config: PipelineConfig, out: TextIO) -> None:
    """Print a human-readable plan for a validated config."""
    out.write(f"{len(config.sources)} source(s), manifest -> {config.resolve(config.manifest)}\n")
    for source in config.sources:
        target = getattr(source, "url", None) or getattr(source, "path", None) or "query"
        out.write(f"\n  {source.name} ({source.type}, {source.format})\n")
        out.write(f"    from: {target}\n")
        max_age = config.effective(source, "max_age")
        out.write(
            f"    timeout: {config.effective(source, 'timeout'):g}s  "
            f"retries: {config.effective(source, 'retries')}  "
            f"max_age: {'unset' if max_age is None else f'{max_age:g}s'}\n"
        )
        if source.id_property:
            out.write(f"    diff key: properties.{source.id_property}\n")
        for step in source.transform:
            out.write(f"    transform: {step.op}\n")
        for output in source.outputs:
            out.write(f"    output: {output.type}\n")
    for output in config.outputs:
        out.write(f"\n  pipeline output: {output.type}\n")


def command_validate(args: argparse.Namespace, out: TextIO) -> int:
    """Execute the ``validate`` command."""
    config = load_config(args.config)
    # Compile every filter expression now so a typo is caught before a 3am cron run.
    for source in config.sources:
        for step in source.transform:
            if step.op == "filter":
                compile_expression(step.expression)
    select_sources(config, [])
    out.write(f"{args.config} is valid.\n")
    describe_config(config, out)
    return EXIT_OK


def _resolve_manifest_path(target: str) -> Path:
    path = Path(target)
    if path.suffix in (".yml", ".yaml"):
        config = load_config(path)
        return config.resolve(config.manifest)
    return path


def command_status(args: argparse.Namespace, out: TextIO) -> int:
    """Execute the ``status`` command."""
    path = _resolve_manifest_path(args.manifest)
    document = load_manifest_document(path)
    if args.json:
        out.write(json.dumps(document, indent=2) + "\n")
        return EXIT_OK

    out.write(f"manifest: {path}\n")
    out.write(f"generated: {document.get('generated_at')}\n")
    totals = document.get("totals") or {}
    out.write(
        f"totals: {totals.get('sources', 0)} sources, "
        f"{totals.get('features', 0)} features, "
        f"{totals.get('changed_sources', 0)} changed, "
        f"{totals.get('stale_sources', 0)} stale\n\n"
    )
    header = f"{'source':<24} {'age':<18} {'features':>9}  state"
    out.write(header + "\n" + "-" * len(header) + "\n")
    stale_any = False
    for name, entry in (document.get("sources") or {}).items():
        age = entry.get("age_seconds")
        age_text = "never fetched" if age is None else humanize_age(float(age))
        flags = []
        if entry.get("stale"):
            flags.append("STALE")
            stale_any = True
        if entry.get("status") == "failed":
            flags.append("FAILED")
        elif entry.get("changed"):
            flags.append("changed")
        else:
            flags.append(entry.get("status", "unknown"))
        out.write(
            f"{name:<24} {age_text:<18} {entry.get('feature_count', 0):>9}  "
            f"{' '.join(flags)}\n"
        )
        diff = entry.get("diff") or {}
        if diff.get("added") or diff.get("removed") or diff.get("modified"):
            out.write(
                f"{'':<24} +{diff.get('added', 0)} / -{diff.get('removed', 0)} / "
                f"~{diff.get('modified', 0)}\n"
            )
        if entry.get("error"):
            out.write(f"{'':<24} error: {entry['error']}\n")
    if stale_any:
        out.write("\nAt least one source is past its max_age.\n")
        return EXIT_VALIDATION_FAILURE
    return EXIT_OK


def command_init(args: argparse.Namespace, out: TextIO) -> int:
    """Execute the ``init`` command."""
    created = write_scaffold(args.directory, force=args.force)
    for path in created:
        out.write(f"created {path}\n")
    if not created:
        out.write(
            "nothing written: those files already exist. Re-run with --force to "
            "overwrite them.\n"
        )
        return EXIT_OK
    out.write(
        "\nNext:\n"
        f"  python -m geo_refresh validate {Path(args.directory) / 'pipeline.yml'}\n"
        f"  python -m geo_refresh run {Path(args.directory) / 'pipeline.yml'}\n"
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    """Entry point. Returns the process exit code instead of raising."""
    stream = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handlers = {
        "run": command_run,
        "validate": command_validate,
        "status": command_status,
        "init": command_init,
    }
    try:
        return handlers[args.command](args, stream)
    except GeoRefreshError as error:
        sys.stderr.write(f"{PROGRAM}: {error}\n")
        return error.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        sys.stderr.write(f"{PROGRAM}: interrupted\n")
        return 130
