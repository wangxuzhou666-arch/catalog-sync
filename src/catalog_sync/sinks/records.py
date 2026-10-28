"""Turning records into a sink batch.

Sinks are field-agnostic: they see rows (dictionaries of JSON values) and how each field is
stored. The schema of the records is the one place that says how, so a batch is built from the
records and their schema, whatever the record type is.
"""

from __future__ import annotations

from collections.abc import Iterable

from catalog_sync.models import Record
from catalog_sync.schema import EntitySchema
from catalog_sync.sinks.base import Batch, BatchOrigin


def record_batch(
    records: Iterable[Record],
    schema: EntitySchema,
    *,
    owned: frozenset[str],
    keep_existing_if_null: frozenset[str] = frozenset(),
    is_full_snapshot: bool = False,
    origin: BatchOrigin | None = None,
) -> Batch:
    return Batch(
        rows=[record.to_dict() for record in records],
        owned=owned,
        kinds=schema.kinds,
        keep_existing_if_null=keep_existing_if_null,
        is_full_snapshot=is_full_snapshot,
        origin=origin,
    )
