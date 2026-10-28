"""The commands that look at past runs and clean up after them: ``runs list|show``, ``cache purge``.

``cache purge`` is the one command that deletes data, so it is written to be asked first: it can
say what it would do (``--dry-run``), it explains every run it keeps, and it leaves an audit line.
Which runs are old enough comes from the retention each source declared in its compliance block,
or from ``--older-than`` when the operator names an age. What may be deleted is decided in
``cache.py``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from catalog_sync.audit import audited
from catalog_sync.cache import (
    PurgePlan,
    SnapshotSize,
    parse_age_days,
    plan_purge,
    purge_runs,
    snapshot_size,
)
from catalog_sync.cli_pipeline import audit_log, candidate_source_names
from catalog_sync.compliance import parse_compliance, stale_runs
from catalog_sync.state import RunRow, Stage, StageStatus, StateStore

if TYPE_CHECKING:
    import argparse

    from catalog_sync.cli import Context

_STARTED_WIDTH = 32


# --- runs list / show -----------------------------------------------------------------------------


def cmd_runs_list(args: argparse.Namespace, context: Context) -> int:
    with StateStore(context.workspace.state_path, clock=context.clock) as store:
        names = candidate_source_names(args.source, context) if args.source else None
        rows = [run for run in store.list_runs() if names is None or run.source in names]
        item_counts = {row.run_id: store.checkpoint(row.run_id).count_items() for row in rows}
        failed = {row.run_id: len(store.checkpoint(row.run_id).failures()) for row in rows}
        dependents = store.dependents()
    if not rows:
        print("no runs yet" if names is None else f"no runs of {args.source}")
        return 0
    print(
        f"{'RUN':<24} {'SOURCE':<16} {'STATUS':<24} {'ITEMS':>6} {'FAILED':>6} {'USED BY':>7}  "
        "STARTED"
    )
    for row in rows:
        print(
            f"{row.run_id:<24} {row.source:<16} {row.status:<24} {item_counts[row.run_id]:>6} "
            f"{failed[row.run_id]:>6} {dependents.get(row.run_id, 0):>7}  {row.started_at}"
        )
    print("USED BY: items whose current raw data lives in that run (such a run is never purged)")
    return 0


def cmd_runs_show(args: argparse.Namespace, context: Context) -> int:
    with StateStore(context.workspace.state_path, clock=context.clock) as store:
        row = store.get_run(args.run_id)
        checkpoint = store.checkpoint(row.run_id)
        counts = checkpoint.stage_counts()
        failures = checkpoint.failures()
        item_count = checkpoint.count_items()
        dependents = store.dependents().get(row.run_id, 0)
    size = snapshot_size(context.workspace.runs_root, row.run_id)
    print(f"run:      {row.run_id}")
    print(f"source:   {row.source}")
    print(f"status:   {row.status}")
    print(f"started:  {row.started_at}")
    print(f"finished: {row.finished_at or '-'}")
    print(f"listing:  {'complete' if row.list_complete else 'incomplete'}")
    print(f"items:    {item_count}")
    for stage in Stage:
        print(f"  {stage:<7}: {format_counts(counts[stage])}")
    print(f"cache:    {_describe_snapshot(size)}")
    print(f"used by:  {dependents} item(s) whose current raw data is in this run")
    for failure in failures:
        print(f"failed {failure.stage} {failure.source_id}: {failure.error}")
    return 0


def format_counts(counts: Mapping[StageStatus, int]) -> str:
    parts = [f"{counts[status]} {status}" for status in StageStatus if status in counts]
    return ", ".join(parts) or "-"


def _describe_snapshot(size: SnapshotSize | None) -> str:
    if size is None:
        return "no snapshot on disk (purged or removed)"
    return f"{size.files} file(s), {_size(size.size_bytes)}"


# --- cache purge ----------------------------------------------------------------------------------


def cmd_cache_purge(args: argparse.Namespace, context: Context) -> int:
    now = context.clock()
    with (
        audited(audit_log(context), "cache_purge", source=args.source) as scope,
        StateStore(context.workspace.state_path, clock=context.clock) as store,
    ):
        runs = store.list_runs()
        if args.source:
            names = candidate_source_names(args.source, context)
            runs = [run for run in runs if run.source in names]
        expired, undeclared = _past_retention(context, runs, args.older_than, now)
        plan = plan_purge(store, expired, include_running=args.include_running)
        sizes = {
            run.run_id: snapshot_size(context.workspace.runs_root, run.run_id)
            for run in plan.purgeable
        }
        result = None
        if not args.dry_run:
            result = purge_runs(store, context.workspace.runs_root, plan.purgeable)
        scope.counts = {
            "runs_purged": len(plan.purgeable),
            "runs_kept_in_use": len(plan.still_needed),
            "runs_kept_running": len(plan.still_running),
            "runs_without_retention": len(undeclared),
            "bytes": sum(size.size_bytes for size in sizes.values() if size is not None),
        }
        if args.dry_run:
            scope.outcome = "dry_run"
    _print_plan(plan, sizes, undeclared, now=now, is_dry_run=args.dry_run)
    if result is not None and result.snapshots_already_gone:
        print(f"  ({result.snapshots_already_gone} snapshot(s) were already gone from disk)")
    return 0


def _past_retention(
    context: Context, runs: Sequence[RunRow], older_than: str | None, now: datetime
) -> tuple[list[RunRow], list[RunRow]]:
    """Runs older than the age given, or than their source's declared retention.

    Returns those, and the runs whose source declared no retention (they are left alone).
    """
    if older_than is not None:
        return stale_runs(runs, parse_age_days(older_than), now=now), []
    retention = _declared_retention(context, {run.source for run in runs})
    expired: list[RunRow] = []
    undeclared: list[RunRow] = []
    for source in sorted({run.source for run in runs}):
        of_source = [run for run in runs if run.source == source]
        if source in retention:
            expired += stale_runs(of_source, retention[source], now=now)
        else:
            undeclared += of_source
    return sorted(expired, key=lambda run: run.started_at, reverse=True), undeclared


def _declared_retention(context: Context, run_sources: Collection[str]) -> dict[str, int]:
    """``retention_days`` per source name that runs are recorded under.

    A run records the plugin's source name (``mock.materials``), the config names an entry
    (``mock-materials``) that runs that plugin. Two entries of one plugin may declare different
    periods and a run does not say which it came from, so the shorter one applies: the
    retention is a ceiling on how long data may be kept. An invalid declaration is an error here
    (exit 4), not a reason to guess.
    """
    config = context.config
    if config is None:
        return {}
    today = context.clock().date()
    found: dict[str, int] = {}
    for entry in config.sources.values():
        if entry.compliance is None:
            continue
        declared = parse_compliance(
            entry.compliance, label=f"sources.{entry.name}", today=today
        ).retention_days
        for source in {entry.name, entry.plugin} & set(run_sources):
            found[source] = min(declared, found.get(source, declared))
    return found


def _print_plan(
    plan: PurgePlan,
    sizes: Mapping[str, SnapshotSize | None],
    undeclared: Sequence[RunRow],
    *,
    now: datetime,
    is_dry_run: bool,
) -> None:
    if not (plan.purgeable or plan.still_needed or plan.still_running or undeclared):
        print("nothing is past its retention")
        return
    if plan.purgeable:
        total = sum(size.size_bytes for size in sizes.values() if size is not None)
        verb = "would purge" if is_dry_run else "purged"
        print(f"{verb} {len(plan.purgeable)} run(s), {_size(total)}:")
        for run in plan.purgeable:
            size = sizes[run.run_id]
            print(
                f"  {run.run_id:<24} {run.source:<16} {_age(run, now):>5}d old  "
                f"{_size(size.size_bytes) if size else 'no snapshot on disk'}"
            )
    else:
        print("no run may be purged")
    if plan.still_needed:
        print(f"kept {len(plan.still_needed)} run(s) past their retention that items depend on:")
        for use in plan.still_needed:
            print(
                f"  {use.run.run_id:<24} {use.run.source:<16} {_age(use.run, now):>5}d old  "
                f"{use.dependents} item(s) not re-pulled since"
            )
        print("  pull the source with --full to move those items into a new run, then purge again")
    if plan.still_running:
        print(f"kept {len(plan.still_running)} run(s) marked running (--include-running to purge):")
        for run in plan.still_running:
            print(f"  {run.run_id:<24} {run.source:<16} {_age(run, now):>5}d old")
    if undeclared:
        sources = ", ".join(sorted({run.source for run in undeclared}))
        print(
            f"{len(undeclared)} run(s) of {sources} were not looked at: no retention is declared "
            "for them in the config (give --older-than, for example 30d)"
        )


def _age(run: RunRow, now: datetime) -> int:
    return (now - datetime.fromisoformat(run.started_at)).days


def _size(size_bytes: int) -> str:
    if size_bytes < 1_000:
        return f"{size_bytes} B"
    if size_bytes < 1_000_000:
        return f"{size_bytes / 1_000:.1f} kB"
    return f"{size_bytes / 1_000_000:.1f} MB"
