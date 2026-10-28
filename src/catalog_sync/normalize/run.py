"""Normalise a whole run from the stored cache, without touching the network.

An incremental run only re-fetches what changed, so for an unchanged item its own directory holds
just the listing row. Its detail and pictures live in the run that last pulled it completely, which
the state database records (``item_versions.run_id``). This module resolves every item through
that record, so ``--run latest`` sees the whole library, not just the delta.

The listing row (and with it the folder paths) is always read from the requested run: an item can
move between folders without its version changing. Detail and pictures are read from the run that
holds them, but only when that pull is still the item's current version; otherwise the item is
reported as skipped rather than normalised from stale detail.

An item that did not finish pulling is skipped too. Normalising it from the listing row alone
would turn every detail-derived field into a null and a load would write those nulls over good
values.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from catalog_sync.errors import ConfigError, SnapshotError
from catalog_sync.models import Record
from catalog_sync.normalize.engine import Provenance, normalize
from catalog_sync.normalize.mapping import MappingSpec
from catalog_sync.snapshot import RunSnapshot
from catalog_sync.state import ItemRow, RunRow, StageStatus, StateStore


@dataclass(frozen=True)
class SkippedItem:
    source_id: str
    reason: str


@dataclass(frozen=True)
class NormalizedRun:
    run: RunRow
    mapping_digest: str
    records: tuple[Record, ...]
    skipped: tuple[SkippedItem, ...]

    @property
    def is_full_snapshot(self) -> bool:
        """True when the records are everything the source listed: the listing finished and no
        item was left out. Only then may a sink call a missing row "gone from the source"."""
        return self.run.list_complete and not self.skipped


def find_run(store: StateStore, source_names: list[str], run_ref: str) -> RunRow:
    """`latest` means the newest finished run of the first of `source_names` that has any.

    A run still marked running is never picked: its snapshot is half written. Name it with
    `--run` to read it anyway.
    """
    if run_ref != "latest":
        return store.get_run(run_ref)
    for name in source_names:
        run = store.latest_run(name, settled_only=True)
        if run is not None:
            return run
    for name in source_names:
        running = store.latest_run(name)
        if running is not None:
            raise ConfigError(
                f"run {running.run_id} of {name} is still running (or was killed before it "
                "finished), so 'latest' skips it; wait for it, or pass --run to read it as is"
            )
    known = sorted({row.source for row in store.list_runs()})
    raise ConfigError(
        f"no run of {' / '.join(source_names)} to normalise; pull first "
        f"(sources with runs: {', '.join(known) or 'none'})"
    )


def normalize_run(
    store: StateStore,
    runs_root: Path,
    run: RunRow,
    spec: MappingSpec,
    *,
    clock: Callable[[], datetime],
) -> NormalizedRun:
    if spec.source != run.source:
        raise ConfigError(
            f"the mapping is written for source {spec.source!r} but run {run.run_id} is from "
            f"{run.source!r}"
        )
    snapshots = _Snapshots(runs_root)
    requested = snapshots.get(run.run_id)
    normalized_at = clock()
    records: list[Record] = []
    skipped: list[SkippedItem] = []
    for row in store.checkpoint(run.run_id).items():
        holder = _holder_run(store, run, row)
        if isinstance(holder, SkippedItem):
            skipped.append(holder)
            continue
        try:
            held = snapshots.get(holder)
        except SnapshotError as exc:
            skipped.append(SkippedItem(row.source_id, f"run {holder} is unusable: {exc}"))
            continue
        bundle = replace(held.bundle(row.source_id), item=requested.get_item(row.source_id))
        provenance = Provenance(
            source_version=row.version,
            raw_ref=f"{holder}:{row.source_id}",
            fetched_at=held.created_at,
            normalized_at=normalized_at,
        )
        records.append(normalize(bundle, spec, provenance))
    return NormalizedRun(run, spec.digest, tuple(records), tuple(skipped))


def _holder_run(store: StateStore, run: RunRow, row: ItemRow) -> str | SkippedItem:
    """The run whose detail and pictures belong to this item, or why there is none."""
    if not row.is_fully_pulled:
        unfinished = sorted(str(stage) for stage, s in row.stages.items() if _is_unfinished(s))
        return SkippedItem(row.source_id, f"not fully pulled ({', '.join(unfinished)})")
    if StageStatus.SKIPPED not in row.stages.values():
        return run.run_id  # pulled in this very run
    stored = store.stored_pull(run.source, row.source_id)
    if stored is None or stored.version != row.version:
        return SkippedItem(
            row.source_id, "unchanged since a pull that is no longer the item's current one"
        )
    return stored.run_id


def _is_unfinished(status: StageStatus) -> bool:
    return status not in (StageStatus.DONE, StageStatus.SKIPPED)


class _Snapshots:
    """Opened run snapshots, so a run holding many unchanged items is opened once."""

    def __init__(self, runs_root: Path) -> None:
        self._runs_root = runs_root
        self._open: dict[str, RunSnapshot] = {}

    def get(self, run_id: str) -> RunSnapshot:
        if run_id not in self._open:
            self._open[run_id] = RunSnapshot.open(self._runs_root / run_id)
        return self._open[run_id]
