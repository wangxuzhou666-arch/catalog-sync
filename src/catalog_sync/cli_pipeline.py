"""The commands that move data: pull (audited), normalize, load, run, and `audit show`.

Each data-touching command writes one audit line when it ends, however it ends (audit.py). The
same building blocks serve the single commands and `run`, which is just pull, normalize and load
in order, so a `run` leaves the same trail as the three commands typed by hand, plus a summary
line of its own.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from catalog_sync import registry
from catalog_sync.audit import AuditLog, audited
from catalog_sync.cache import plan_purge
from catalog_sync.cli_sources import (
    build_source,
    check_gate,
    credential_fingerprint,
    describe_source,
)
from catalog_sync.compliance import Compliance, SourceGate, parse_compliance, stale_runs
from catalog_sync.config import DEFAULT_MAX_FAILED_RATIO, ProfileConfig
from catalog_sync.errors import EXIT_COMPLETED_WITH_FAILURES, CatalogSyncError, ConfigError
from catalog_sync.loading import LoadOutcome, load_normalized, sink_config
from catalog_sync.normalize.mapping import MappingSpec, load_mapping
from catalog_sync.normalize.output import (
    NormalizedFile,
    from_run,
    read_normalized,
    write_normalized,
)
from catalog_sync.normalize.run import NormalizedRun, find_run, normalize_run
from catalog_sync.puller import Puller, PullReport
from catalog_sync.sources.base import Source
from catalog_sync.state import Stage, StageStatus, StateStore

if TYPE_CHECKING:
    from catalog_sync.cli import Context

SourceBuilder = Callable[[str, Mapping[str, str], "Context"], Source]
_MAX_SKIPPED_SHOWN = 5


def audit_log(context: Context) -> AuditLog:
    return AuditLog(context.workspace.audit_path, clock=context.clock)


# --- pull ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PullOutcome:
    report: PullReport
    gate: SourceGate


def pull_with_audit(
    context: Context,
    name: str,
    overrides: Mapping[str, str],
    *,
    build: SourceBuilder = build_source,
    stages: Collection[Stage],
    resume: str | None,
    max_items: int | None,
    incremental: bool,
) -> PullOutcome:
    resolved = describe_source(name, overrides, context)
    with audited(audit_log(context), "pull", source=name) as scope:
        gate = check_gate(resolved, context, should_warn=True)
        scope.compliance_sha256 = gate.compliance.digest if gate.compliance else None
        source = build(name, overrides, context)
        scope.source = source.name
        scope.credential_hash = credential_fingerprint(resolved, context)
        with StateStore(context.workspace.state_path, clock=context.clock) as store:
            puller = Puller(source, store, context.workspace.runs_root, clock=context.clock)
            report = puller.pull(
                stages=stages, resume=resume, max_items=max_items, incremental=incremental
            )
        scope.run_id = report.run_id
        scope.counts = {
            "items": report.item_count,
            "detail_fetches": report.detail_fetches,
            "asset_fetches": report.asset_fetches,
            "failed_items": len(report.failures),
        }
        if report.failures:
            scope.outcome = "ok_with_failures"
    return PullOutcome(report, gate)


# --- normalize ----------------------------------------------------------------------------------


def candidate_source_names(typed: str, context: Context) -> list[str]:
    """Names `latest` is looked up under: what was typed, then the plugin a config entry runs."""
    config = context.config
    if config is not None and typed in config.sources:
        return [typed, config.sources[typed].plugin]
    return [typed]


def declared_digest(context: Context, names: Sequence[str]) -> str | None:
    """The sha256 of the compliance declaration of the config entry behind any of `names`.

    Best effort, for the audit line of an offline command: a declaration that does not validate
    is reported by the commands that need one (pull), not by normalising what is already cached.
    """
    config = context.config
    if config is None:
        return None
    for entry in config.sources.values():
        if entry.compliance is not None and (entry.name in names or entry.plugin in names):
            try:
                return parse_compliance(
                    entry.compliance, label=f"sources.{entry.name}", today=context.clock().date()
                ).digest
            except CatalogSyncError:
                return None
    return None


def normalize_with_audit(
    context: Context,
    *,
    source_names: list[str],
    run_ref: str,
    spec: MappingSpec,
    out: Path | None,
) -> tuple[NormalizedRun, NormalizedFile]:
    with (
        audited(audit_log(context), "normalize", source=source_names[0]) as scope,
        StateStore(context.workspace.state_path, clock=context.clock) as store,
    ):
        run = find_run(store, source_names, run_ref)
        scope.source, scope.run_id = run.source, run.run_id
        scope.compliance_sha256 = declared_digest(context, [run.source, *source_names])
        normalized = normalize_run(
            store, context.workspace.runs_root, run, spec, clock=context.clock
        )
        file = from_run(normalized, spec)
        if out is not None:
            write_normalized(out, file)
        scope.counts = {
            "records": len(normalized.records),
            "skipped": len(normalized.skipped),
            "records_with_issues": sum(1 for r in normalized.records if r.issues),
        }
    return normalized, file


def packaged_mapping(names: Sequence[str]) -> Path | None:
    """The mapping the first of these source plugins ships for itself, if any does."""
    for name in names:
        path = registry.default_mapping(name)
        if path is not None:
            return path
    return None


def cmd_normalize(args: argparse.Namespace, context: Context) -> int:
    if args.mapping is not None:
        mapping_path = Path(args.mapping)
    else:
        packaged = packaged_mapping(candidate_source_names(args.source, context))
        if packaged is None:
            raise ConfigError(
                f"no --mapping given, and {args.source!r} is not a connector that ships one "
                "(`replay` and a plugin without connector info do not): pass --mapping FILE"
            )
        mapping_path = packaged
    spec = load_mapping(mapping_path)
    out = Path(args.out)
    normalized, _ = normalize_with_audit(
        context,
        source_names=candidate_source_names(args.source, context),
        run_ref=args.run,
        spec=spec,
        out=out,
    )
    print(
        f"normalized run {normalized.run.run_id} ({normalized.run.source}): "
        f"{len(normalized.records)} record(s) -> {out}"
    )
    _print_normalize_details(normalized)
    return 0


def _print_normalize_details(normalized: NormalizedRun) -> None:
    for skipped in normalized.skipped[:_MAX_SKIPPED_SHOWN]:
        print(f"  skipped {skipped.source_id}: {skipped.reason}", file=sys.stderr)
    if len(normalized.skipped) > _MAX_SKIPPED_SHOWN:
        extra = len(normalized.skipped) - _MAX_SKIPPED_SHOWN
        print(f"  ... and {extra} more skipped item(s)", file=sys.stderr)
    kinds = Counter(
        ":".join(issue.split(":")[:2]) for record in normalized.records for issue in record.issues
    )
    if kinds:
        shown = ", ".join(f"{kind} x{count}" for kind, count in sorted(kinds.items()))
        print(f"  issues: {shown}")


# --- load ---------------------------------------------------------------------------------------


def load_with_audit(
    context: Context, sink_name: str, normalized: NormalizedFile, *, dry_run: bool
) -> LoadOutcome:
    with audited(audit_log(context), "load", source=normalized.source) as scope:
        scope.run_id = normalized.run_id
        scope.compliance_sha256 = declared_digest(context, [normalized.source])
        entry = sink_config(context.config, sink_name)
        scope.sinks.append(sink_name)
        base_dir = context.config.base_dir if context.config else Path.cwd()
        outcome = load_normalized(
            entry,
            normalized,
            base_dir=base_dir,
            workspace=context.workspace,
            salt_path=context.workspace.hash_salt_path,
            dry_run=dry_run,
        )
        report = outcome.report
        scope.counts = {
            "inserted": report.inserted,
            "updated": report.updated,
            "unchanged": report.unchanged,
            "absent": len(report.absent),
        }
        if dry_run:
            scope.outcome = "dry_run"
    return outcome


def cmd_load(args: argparse.Namespace, context: Context) -> int:
    normalized = read_normalized(Path(args.input))
    outcome = load_with_audit(context, args.sink, normalized, dry_run=args.dry_run)
    print_load(outcome)
    return 0


def print_load(outcome: LoadOutcome) -> None:
    report = outcome.report
    label = "dry run, nothing written" if outcome.is_dry_run else "written"
    print(f"load -> {outcome.sink_name} ({outcome.kind}), {label}:")
    print(f"  inserted {report.inserted}, updated {report.updated}, unchanged {report.unchanged}")
    for note in report.notes:
        print(f"  {note}")
    if report.absent:
        print(f"  {len(report.absent)} stored row(s) no longer in the source (reported, kept)")
    if outcome.backup is not None:
        print(f"  backup: {outcome.backup.describe()}")
    if outcome.sensitive_fields:
        print(f"  sensitive fields ({outcome.policy}): {', '.join(outcome.sensitive_fields)}")


# --- run ----------------------------------------------------------------------------------------


def lookup_profile(context: Context, name: str, *, command: str = "run") -> ProfileConfig:
    config = context.config
    if config is None:
        raise ConfigError(f"`{command}` needs a config file with a [profiles.NAME] entry")
    try:
        return config.profiles[name]
    except KeyError:
        known = ", ".join(sorted(config.profiles)) or "none"
        raise ConfigError(f"unknown profile {name!r} (defined: {known})") from None


def ratio_argument(text: str) -> float:
    """``--max-failed-ratio``: a share from 0 to 1 (``0.1`` is ten percent)."""
    try:
        ratio = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number from 0 to 1, got {text!r}") from None
    if not 0 <= ratio <= 1:  # also false for nan
        raise argparse.ArgumentTypeError(f"expected a number from 0 to 1, got {text!r}")
    return ratio


def add_max_failed_ratio_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-failed-ratio",
        type=ratio_argument,
        default=None,
        metavar="RATIO",
        help="the share of fetched items (0 to 1) that may fail before the run counts as failed "
        f"(exit 1) instead of completed with item failures (exit {EXIT_COMPLETED_WITH_FAILURES}); "
        f"default: the profile's max_failed_ratio, else {DEFAULT_MAX_FAILED_RATIO}",
    )


def cmd_run(args: argparse.Namespace, context: Context) -> int:
    return run_profile(context, args.profile, max_failed_ratio=args.max_failed_ratio)


def run_profile(
    context: Context, profile_name: str, *, max_failed_ratio: float | None = None
) -> int:
    """The body of `run`, which `schedule` calls once per cycle.

    Exit 0: every item was pulled. Exit 5 (completed with item failures): the run finished, some
    items failed, and they are no more than `max_failed_ratio` of the items it tried to fetch;
    what did arrive was normalised and loaded. Exit 1: more than that failed, so the run is
    treated as a failure (the data that did arrive is still loaded). `max_failed_ratio` overrides
    the profile's.
    """
    profile = lookup_profile(context, profile_name)
    limit = profile.max_failed_ratio if max_failed_ratio is None else max_failed_ratio
    mapping_path = profile.mapping or packaged_mapping(
        candidate_source_names(profile.source, context)
    )
    if mapping_path is None:
        raise ConfigError(
            f"profile {profile.name!r} has no 'mapping', and its source {profile.source!r} is not "
            f"a connector that ships one (`replay` and a plugin without connector info do not): "
            'set mapping = "FILE" in the profile'
        )
    spec = load_mapping(mapping_path)  # a broken mapping is found before any request is made
    with audited(audit_log(context), "run", profile=profile.name, source=profile.source) as scope:
        pulled = pull_with_audit(
            context,
            profile.source,
            {},
            stages=(Stage.LIST, Stage.DETAIL, Stage.ASSETS),
            resume=None,
            max_items=None,
            incremental=profile.incremental,
        )
        report = pulled.report
        scope.source, scope.run_id = report.source, report.run_id
        scope.compliance_sha256 = pulled.gate.compliance.digest if pulled.gate.compliance else None
        print_pull_summary(report)
        _warn_about_old_cache(context, report.source, pulled.gate.compliance)
        _, file = normalize_with_audit(
            context,
            source_names=[report.source],
            run_ref=report.run_id,
            spec=spec,
            out=None,
        )
        for sink_name in profile.sinks:
            outcome = load_with_audit(context, sink_name, file, dry_run=False)
            print_load(outcome)
            scope.sinks.append(sink_name)
        scope.counts = {"records": len(file.records), "failed_items": len(report.failures)}
        exit_code = _exit_code_for_failures(report, limit)
        if exit_code == 1:
            scope.outcome = "failed"
        elif exit_code == EXIT_COMPLETED_WITH_FAILURES:
            scope.outcome = "ok_with_failures"
    return exit_code


def failed_item_ratio(report: PullReport) -> tuple[int, int]:
    """(items that failed, items the run tried to fetch).

    An item an incremental pull found unchanged was skipped, not attempted: counting it would let
    a total outage of the few items that did change look like a small share of the catalogue.
    """
    failed = len({failure.source_id for failure in report.failures})
    skipped = report.stage_counts[Stage.DETAIL].get(StageStatus.SKIPPED, 0)
    return failed, report.item_count - skipped


def _exit_code_for_failures(report: PullReport, limit: float) -> int:
    if not report.failures:
        return 0
    failed, attempted = failed_item_ratio(report)
    ratio = failed / attempted  # failures exist, so something was attempted
    summary = f"{failed} of {attempted} fetched item(s) failed ({ratio:.1%})"
    if ratio > limit:
        print(
            f"error: {summary}, over the limit of {limit:.1%} (--max-failed-ratio); "
            "the run counts as failed",
            file=sys.stderr,
        )
        return 1
    print(
        f"warning: {summary}, within the limit of {limit:.1%} (--max-failed-ratio); "
        f"completed with item failures (exit {EXIT_COMPLETED_WITH_FAILURES})",
        file=sys.stderr,
    )
    return EXIT_COMPLETED_WITH_FAILURES


def print_pull_summary(report: PullReport) -> None:
    status = report.status.replace("_", " ")
    print(f"run {report.run_id} ({report.source}): {status}, {report.item_count} item(s)")
    for failure in report.failures[:_MAX_SKIPPED_SHOWN]:
        print(f"  failed {failure.stage} {failure.source_id}: {failure.error}", file=sys.stderr)


def _warn_about_old_cache(context: Context, source: str, compliance: Compliance | None) -> None:
    if compliance is None:
        return
    with StateStore(context.workspace.state_path, clock=context.clock) as store:
        runs = [run for run in store.list_runs() if run.source == source]
        expired = stale_runs(runs, compliance.retention_days, now=context.clock())
        plan = plan_purge(store, expired)
    if not expired:
        return
    print(
        f"warning: the cache holds {len(expired)} run(s) of {source} older than the declared "
        f"retention of {compliance.retention_days} day(s) (oldest: {expired[-1].run_id}). "
        f"`catalog-sync cache purge --dry-run` shows what would go ({len(plan.purgeable)} "
        f"run(s)); {len(plan.still_needed)} are kept because unchanged items still depend on "
        "them, and a pull with --full moves those items on.",
        file=sys.stderr,
    )


# --- audit show ---------------------------------------------------------------------------------


def cmd_audit_show(args: argparse.Namespace, context: Context) -> int:
    entries = AuditLog(context.workspace.audit_path).read()
    shown = entries[-args.last :] if args.last else entries
    if args.json:
        for entry in shown:
            print(json.dumps(entry, ensure_ascii=False))
        return 0
    if not shown:
        print("no audit entries yet")
        return 0
    print(f"{'TIME':<32} {'COMMAND':<10} {'OUTCOME':<22} {'SOURCE':<16} {'RUN':<24} COUNTS")
    for entry in shown:
        counts = entry.get("counts")
        text = ", ".join(f"{k}={v}" for k, v in counts.items()) if isinstance(counts, dict) else ""
        print(
            f"{_cell(entry, 'ts'):<32} {_cell(entry, 'command'):<10} "
            f"{_cell(entry, 'outcome'):<22} {_cell(entry, 'source'):<16} "
            f"{_cell(entry, 'run_id'):<24} {text}"
        )
    return 0


def _cell(entry: Mapping[str, object], key: str) -> str:
    value = entry.get(key)
    return "-" if value is None else str(value)
