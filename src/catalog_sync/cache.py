"""Retention of the raw-response cache: what is past its declared retention, and what may go.

A run is a directory of raw responses (``cache/runs/<run id>/``) plus bookkeeping rows in the
state database. The cache holds copies of a third party's data, so the operator declares how long
it may be kept (``retention_days`` in the source's compliance block) and this module enforces it.

Two kinds of run are never deleted, however old they are:

* a run that unchanged items still depend on. An incremental pull does not fetch an item whose
  listing row did not change; that item's detail and pictures stay in the run that last pulled it
  completely (``item_versions.run_id``). Deleting that run would make the next incremental pull
  believe the item is current while its raw data is gone. Such runs are reported as retained, with
  the number of items that hold them; ``pull --full`` moves the items into a new run, after which
  the old one can go.
* a run still marked ``running``: either another process is writing to it, or it was killed. The
  operator decides (``--include-running``) because deleting a directory under a live pull breaks it.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from catalog_sync.errors import ConfigError, SnapshotError
from catalog_sync.snapshot import is_valid_run_id
from catalog_sync.state import RunRow, RunStatus, StateStore

_AGE_IN_DAYS = re.compile(r"(\d+)d")


def parse_age_days(text: str) -> int:
    """``30d`` as 30. Retention is declared in whole days, so that is the unit here too."""
    match = _AGE_IN_DAYS.fullmatch(text.strip())
    if match is None or int(match.group(1)) == 0:
        raise ConfigError(f"an age is a positive number of days written like 30d, not {text!r}")
    return int(match.group(1))


@dataclass(frozen=True)
class RunUse:
    run: RunRow
    dependents: int  # items whose current raw data lives in this run


@dataclass(frozen=True)
class PurgePlan:
    purgeable: tuple[RunRow, ...]
    still_needed: tuple[RunUse, ...]
    still_running: tuple[RunRow, ...]


def plan_purge(
    store: StateStore, expired: Sequence[RunRow], *, include_running: bool = False
) -> PurgePlan:
    """Sort runs that are past their retention into what may be deleted and what must stay."""
    dependents = store.dependents()
    purgeable: list[RunRow] = []
    needed: list[RunUse] = []
    running: list[RunRow] = []
    for run in expired:
        if run.run_id in dependents:
            needed.append(RunUse(run, dependents[run.run_id]))
        elif run.status is RunStatus.RUNNING and not include_running:
            running.append(run)
        else:
            purgeable.append(run)
    return PurgePlan(tuple(purgeable), tuple(needed), tuple(running))


@dataclass(frozen=True)
class SnapshotSize:
    files: int
    size_bytes: int


def snapshot_size(runs_root: Path, run_id: str) -> SnapshotSize | None:
    """What a run's directory holds, or None when it is not there (already deleted)."""
    directory = _run_directory(runs_root, run_id)
    if not directory.is_dir():
        return None
    files = [path for path in directory.rglob("*") if path.is_file()]
    return SnapshotSize(len(files), sum(path.stat().st_size for path in files))


@dataclass(frozen=True)
class PurgeResult:
    runs: int
    files: int
    size_bytes: int
    snapshots_already_gone: int


def purge_runs(store: StateStore, runs_root: Path, runs: Sequence[RunRow]) -> PurgeResult:
    """Claim each run, delete its directory, then delete its rows, in that order.

    The claim comes first: ``mark_purging`` checks that no item depends on the run and flips the
    run to ``purging`` in one transaction, so a pull that tries to resume it afterwards is refused
    instead of writing into a directory that is being removed. A refusal deletes nothing.
    The directory goes before the rows: if the process dies in between, the run is still listed
    (as ``purging``) and the next purge finishes it. The other order would leave a directory
    nothing lists and nothing deletes.
    """
    files = size_bytes = gone = 0
    for run in runs:
        directory = _run_directory(runs_root, run.run_id)  # a bad id or symlink: refused unclaimed
        store.mark_purging(run.run_id, was=run.status)
        size = snapshot_size(runs_root, run.run_id)
        if size is None:
            gone += 1
        else:
            files, size_bytes = files + size.files, size_bytes + size.size_bytes
            _delete_directory(directory)
        store.delete_run(run.run_id)  # checks again, inside its own write transaction
    return PurgeResult(len(runs), files, size_bytes, gone)


def _run_directory(runs_root: Path, run_id: str) -> Path:
    """The directory of `run_id`, checked to be a plain child of `runs_root`.

    Run ids come from the state database; a corrupted or edited one must not be able to point
    a recursive delete anywhere else.
    """
    if not is_valid_run_id(run_id):
        raise SnapshotError(f"refusing to touch the directory of an invalid run id {run_id!r}")
    directory = runs_root / run_id
    if directory.is_symlink():
        raise SnapshotError(f"{run_id} is a symbolic link, not a run directory; not following it")
    return directory


def _delete_directory(directory: Path) -> None:
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        raise SnapshotError(
            f"cannot delete {directory.name} completely ({exc.strerror}); nothing else was changed "
            "for this run, so a later purge can finish it"
        ) from exc
