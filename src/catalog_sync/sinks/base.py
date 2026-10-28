"""What a sink has to provide, and the partial-upsert contract every sink honours.

A sink knows nothing about what a row means. It sees a key, ``(source, source_id)``, and a
dictionary of field values in JSON form; the batch says which fields it owns and how each one is
stored (``FieldKind``). That is what lets one contract suite, and one set of sinks, serve any
record type.

Contract (checked by ``catalog_sync.testing.sink_contract``, which external sinks reuse):

* Fields outside ``Batch.owned`` are never touched, whatever the incoming row holds.
* An owned field whose incoming value is null is written as null, unless it is listed in
  ``keep_existing_if_null``, in which case a non-null stored value survives (COALESCE). An empty
  list or object counts as null there: "the source gave nothing".
* Rows are never deleted or replaced wholesale. Rows the source stopped returning are reported
  in ``WriteReport.absent`` and left alone. (SQL sinks use ``INSERT ... ON CONFLICT DO UPDATE``;
  ``INSERT OR REPLACE`` is delete-then-insert and would cascade-delete child rows.)
* ``backup()`` must be called after ``open()`` and before the first ``upsert()``. A dry run
  (``upsert(..., dry_run=True)``) needs no backup because it writes nothing.

Two things a sink that stores nothing itself (the HTTP sink hands rows to someone else's platform)
cannot do, and says so rather than pretending:

* ``backup_supported = False``: ``backup()`` still has to be called first, but its receipt is a
  documented no-op (``BackupReceipt.note`` says whose responsibility recovery then is), not a copy
  of rows. The contract suite checks that declaration instead of reading backed-up rows.
* ``dry_run_predicts_counts = False``: a dry run reports no inserted/updated/unchanged counts,
  because only the receiving side knows them, and explains itself in ``WriteReport.notes``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypeAlias

from catalog_sync.models import RecordKey
from catalog_sync.schema import KEY_FIELDS, FieldKind

Row: TypeAlias = Mapping[str, object]


def row_key(row: Row) -> RecordKey:
    source, source_id = row.get("source"), row.get("source_id")
    if not (isinstance(source, str) and isinstance(source_id, str)):
        raise TypeError("a row needs string 'source' and 'source_id' values")
    return (source, source_id)


@dataclass(frozen=True)
class BatchOrigin:
    """Where a batch came from, for the sinks that pass it on to a system that wants to know."""

    entity: str  # the schema's name: "material", "supplier", ...
    schema_version: int
    run_id: str


@dataclass(frozen=True)
class Batch:
    rows: Sequence[Row]
    owned: frozenset[str]
    kinds: Mapping[str, FieldKind]  # must cover every owned field
    keep_existing_if_null: frozenset[str] = frozenset()
    # True only when `rows` is everything the source currently returns. Without it a sink
    # cannot tell "gone from the source" from "not part of this incremental batch".
    is_full_snapshot: bool = False
    # Only sinks that forward the batch (HTTP) read this; the others ignore it.
    origin: BatchOrigin | None = None

    def __post_init__(self) -> None:
        if self.owned & KEY_FIELDS:
            raise ValueError(f"identity fields cannot be owned: {sorted(self.owned & KEY_FIELDS)}")
        if not self.keep_existing_if_null <= self.owned:
            stray = sorted(self.keep_existing_if_null - self.owned)
            raise ValueError(f"keep_existing_if_null must be owned fields, got {stray}")
        untyped = sorted(self.owned - set(self.kinds))
        if untyped:
            raise ValueError(f"owned fields without a kind: {untyped}")
        keys = [row_key(row) for row in self.rows]
        if len(set(keys)) != len(keys):
            raise ValueError("a batch may contain each (source, source_id) only once")
        for row in self.rows:
            for name in sorted(self.owned):
                if name not in row:
                    raise ValueError(f"row {row_key(row)} lacks the owned field {name!r}")
                check_value(name, self.kinds[name], row[name])


def check_value(name: str, kind: FieldKind, value: object) -> None:
    """Refuse a value a sink could not store as `kind`. Booleans are not numbers here."""
    if value is None or kind is FieldKind.JSON:
        return
    is_fitting = False
    if not isinstance(value, bool):
        if kind is FieldKind.TEXT:
            is_fitting = isinstance(value, str)
        elif kind is FieldKind.TIMESTAMP:
            is_fitting = isinstance(value, str) and _is_aware_timestamp(value)
        elif kind is FieldKind.INTEGER:
            is_fitting = isinstance(value, int)
        else:
            is_fitting = isinstance(value, (int, float)) and math.isfinite(value)
    if not is_fitting:
        raise ValueError(f"field {name!r} is declared {kind} but holds {type(value).__name__}")


def _is_aware_timestamp(text: str) -> bool:
    """A timestamp without an offset would mean a different instant on every machine."""
    try:
        return datetime.fromisoformat(text).tzinfo is not None
    except ValueError:
        return False


@dataclass(frozen=True)
class BackupReceipt:
    """Where the pre-load state went: a file (`path`) or a table (`table`), neither when the
    sink had nothing stored yet. A sink with `backup_supported = False` takes no copy and says in
    `note` why and whose job recovery is."""

    path: Path | None
    rows_before: int
    table: str | None = None
    note: str | None = None

    @property
    def location(self) -> str:
        if self.path is not None:
            return self.path.name
        return self.table or "nothing stored yet"

    def describe(self) -> str:
        """One line for the load report."""
        if self.note is not None:
            return self.note
        return f"{self.location} ({self.rows_before} row(s) before)"


@dataclass(frozen=True)
class WriteReport:
    inserted: int
    updated: int
    unchanged: int
    absent: tuple[RecordKey, ...] = ()
    notes: tuple[str, ...] = ()  # lines for the person reading the load report


class Sink(Protocol):
    """A place normalised rows are loaded into. Implementations: JSONL, CSV, SQLite, Postgres,
    HTTP."""

    backup_supported: bool
    dry_run_predicts_counts: bool

    def open(self) -> None: ...

    def backup(self) -> BackupReceipt: ...

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> WriteReport:
        """Apply `batch`. With `dry_run`, report exactly what would happen and write nothing."""
        ...

    def close(self) -> None: ...
