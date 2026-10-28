"""The built-in `material` schema as seen by sinks.

Kept for adapters written when MaterialRecord was the only record type: `material_batch` is
`record_batch` with the material schema filled in. New code uses `record_batch` and the schema of
the records it holds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from catalog_sync.models import MATERIAL_SCHEMA, Record
from catalog_sync.schema import FieldKind
from catalog_sync.sinks.base import Batch
from catalog_sync.sinks.records import record_batch

MATERIAL_KINDS: Mapping[str, FieldKind] = MATERIAL_SCHEMA.kinds


def material_batch(
    records: Iterable[Record],
    *,
    owned: frozenset[str],
    keep_existing_if_null: frozenset[str] = frozenset(),
    is_full_snapshot: bool = False,
) -> Batch:
    return record_batch(
        records,
        MATERIAL_SCHEMA,
        owned=owned,
        keep_existing_if_null=keep_existing_if_null,
        is_full_snapshot=is_full_snapshot,
    )
