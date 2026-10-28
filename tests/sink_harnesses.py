"""Harnesses that plug each built-in sink into the contract suite, and the mutants that prove the
suite can fail.

A mutant is a real sink wrapped so that it breaks exactly one rule of the contract. The suite is
only worth having if each broken rule turns the check that guards it red (CODING-STYLE R.7).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from catalog_sync import registry
from catalog_sync.errors import SinkError
from catalog_sync.models import MATERIAL_SCHEMA, RecordKey
from catalog_sync.schema import load_schema_file
from catalog_sync.sinks.base import BackupReceipt, Batch, Sink, WriteReport
from catalog_sync.sinks.csv import CsvSink
from catalog_sync.sinks.jsonl import JsonlSink
from catalog_sync.sinks.postgres import PostgresSink
from catalog_sync.sinks.sqlite import SqliteSink, read_table
from catalog_sync.testing.sink_contract import (
    CONTRACT_CHECKS,
    CONTRACT_KINDS,
    GENERIC_SHAPE,
    ContractCheck,
    ContractShape,
    HarnessFactory,
    Rows,
    SinkHarness,
    shape_of,
)

SinkName = Literal["jsonl", "csv", "sqlite"]
FileSink = JsonlSink | CsvSink | SqliteSink
# The mutants that only change a batch or skip a call work on any sink; the ones that reach into a
# file (alias_backup, deletes_absent) are for FileSink only.
AnySink = FileSink | PostgresSink
SINK_NAMES: tuple[SinkName, ...] = ("jsonl", "csv", "sqlite")

# The checks run against the invented fields and against the real columns of two entity schemas:
# every column of `material` and of `supplier` (an integer, a timestamp, a JSON object) is written
# and read back by every sink.
SHAPES: dict[str, ContractShape] = {
    "generic": GENERIC_SHAPE,
    "material": shape_of(MATERIAL_SCHEMA),
    "supplier": shape_of(load_schema_file(registry.schema_path("supplier"))),
}


def files_in(directory: Path) -> list[Path]:
    """What a sink left in `directory`, not counting its lock file (a lock file is not data)."""
    return sorted(path for path in directory.iterdir() if path.suffix != ".lock")


def _clock() -> Callable[[], datetime]:
    ticks = itertools.count()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return lambda: start + timedelta(microseconds=next(ticks))


def make_sink(name: SinkName, workdir: Path) -> FileSink:
    clock = _clock()
    if name == "jsonl":
        return JsonlSink(workdir / "out" / "catalog.jsonl", clock=clock)
    if name == "csv":
        return CsvSink(workdir / "out" / "catalog.csv", clock=clock)
    return SqliteSink(workdir / "out" / "catalog.db", clock=clock)


def read_stored(name: SinkName, path: Path, shape: ContractShape = GENERIC_SHAPE) -> Rows:
    if not path.exists():
        return {}
    if name == "jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return {(row["source"], row["source_id"]): row for row in rows}
    if name == "csv":
        return CsvSink(path).read_rows(shape.kinds)
    return read_table(path)


def harness_for(
    name: SinkName,
    wrap: Callable[[FileSink], Sink] | None = None,
    shape: ContractShape = GENERIC_SHAPE,
) -> HarnessFactory:
    def make(workdir: Path) -> SinkHarness:
        real = make_sink(name, workdir)
        live_path = real.path
        sink = wrap(real) if wrap else real

        def read_backup(receipt: BackupReceipt) -> Rows:
            assert receipt.path is not None, "a sink with rows must produce a backup file"
            return read_stored(name, receipt.path, shape)

        return SinkHarness(sink, lambda: read_stored(name, live_path, shape), read_backup, shape)

    return make


# --- mutants ------------------------------------------------------------------------------------


@dataclass
class Mutant:
    """Wraps a sink and breaks one contract rule; every field defaults to 'behave correctly'."""

    inner: AnySink
    name: SinkName | Literal["postgres"]
    rewrite: Callable[[Batch], Batch] | None = None
    auto_backup: bool = False  # take the backup the caller forgot
    ignore_dry_run: bool = False
    alias_backup: bool = False  # hand out the live file as its own backup
    deletes_absent: bool = False

    @property
    def backup_supported(self) -> bool:
        return self.inner.backup_supported

    @property
    def dry_run_predicts_counts(self) -> bool:
        return self.inner.dry_run_predicts_counts

    def open(self) -> None:
        self.inner.open()

    def close(self) -> None:
        self.inner.close()

    def backup(self) -> BackupReceipt:
        receipt = self.inner.backup()
        if self.alias_backup and receipt.path is not None and isinstance(self.inner, FileSinkTypes):
            return BackupReceipt(self.inner.path, receipt.rows_before)
        return receipt

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> WriteReport:
        if self.auto_backup or self.ignore_dry_run:
            self.inner.backup()
        if self.rewrite is not None:
            batch = self.rewrite(batch)
        report = self.inner.upsert(batch, dry_run=dry_run and not self.ignore_dry_run)
        if self.deletes_absent:
            _delete_rows(self.inner, self.name, report.absent)
        return report


FileSinkTypes = (JsonlSink, CsvSink, SqliteSink)


def _delete_rows(sink: FileSink, name: SinkName, keys: tuple[RecordKey, ...]) -> None:
    if not keys:
        return
    if name == "jsonl":
        assert isinstance(sink, JsonlSink)
        rows = sink._read_rows()
        for key in keys:
            del rows[key]
        sink._write_rows(rows)
    elif name == "csv":
        assert isinstance(sink, CsvSink)
        header, table = sink._read_table()
        for key in keys:
            del table[key]
        sink._write_table(header, table)
    else:
        assert isinstance(sink, SqliteSink)
        conn = sink._connection()
        for source, source_id in keys:
            conn.execute(
                "DELETE FROM materials WHERE source = ? AND source_id = ?",
                (source, source_id),
            )


def _replace_all(batch: Batch) -> Batch:
    """INSERT OR REPLACE semantics: whatever the row holds is written, owned or not."""
    return replace(batch, owned=frozenset(CONTRACT_KINDS), keep_existing_if_null=frozenset())


def _forget_keep(batch: Batch) -> Batch:
    return replace(batch, keep_existing_if_null=frozenset())


def _always_full(batch: Batch) -> Batch:
    return replace(batch, is_full_snapshot=True)


# (description, how to build it, the check that must turn red)
MutantBuilder = Callable[[FileSink, SinkName], Mutant]


def honest(sink: FileSink, name: SinkName) -> Mutant:
    """The control: a wrapper that breaks nothing, so a red result is the mutation's doing."""
    return Mutant(sink, name)


MUTANTS: list[tuple[str, MutantBuilder, str]] = [
    (
        "replaces rows wholesale",
        lambda sink, name: Mutant(sink, name, rewrite=_replace_all),
        "check_unowned_fields_untouched",
    ),
    (
        "ignores keep_existing_if_null",
        lambda sink, name: Mutant(sink, name, rewrite=_forget_keep),
        "check_keep_existing_if_null",
    ),
    (
        "deletes rows the source no longer returns",
        lambda sink, name: Mutant(sink, name, deletes_absent=True),
        "check_never_deletes_rows",
    ),
    (
        "lets a load run without a backup",
        lambda sink, name: Mutant(sink, name, auto_backup=True),
        "check_backup_precedes_upsert",
    ),
    (
        "hands out the live file as its backup",
        lambda sink, name: Mutant(sink, name, alias_backup=True),
        "check_backup_precedes_upsert",
    ),
    (
        "reports absent rows for incremental batches",
        lambda sink, name: Mutant(sink, name, rewrite=_always_full),
        "check_never_deletes_rows",
    ),
    (
        "writes during a dry run",
        lambda sink, name: Mutant(sink, name, ignore_dry_run=True),
        "check_dry_run_writes_nothing",
    ),
]


def failing_checks(name: SinkName, tmp_path: Path, build: MutantBuilder) -> set[str]:
    """Names of the contract checks that fail for the sink wrapped by `build`."""
    failed: set[str] = set()
    for number, check in enumerate(CONTRACT_CHECKS):
        factory = harness_for(name, lambda sink: build(sink, name))
        if check_fails(check, factory, tmp_path / str(number)):
            failed.add(check.__name__)
    return failed


def check_fails(check: ContractCheck, factory: HarnessFactory, workdir: Path) -> bool:
    try:
        check(factory, workdir)
    except (AssertionError, SinkError):
        return True
    return False
