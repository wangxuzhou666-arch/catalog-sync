"""A small table of the first normalised records, for the person setting a source up.

It answers "is this what I expected to get?" before anything is written anywhere. Cells are masked
by the same ``redact_text`` as every other output, long values are cut, and a field the mapping
marks ``sensitive`` is not shown at all (only its name is, as hidden).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from catalog_sync.models import Record
from catalog_sync.redact import redact_text
from catalog_sync.schema import PROVENANCE_FIELDS, EntitySchema

MAX_ROWS = 5
MAX_COLUMNS = 6
CELL_WIDTH = 22
_ELLIPSIS = "..."


def preview_lines(
    records: Sequence[Record],
    schema: EntitySchema,
    *,
    owned: frozenset[str],
    sensitive: frozenset[str],
) -> list[str]:
    """The table, then a line naming the hidden fields and one counting the records not shown."""
    if not records:
        return ["  (no records)"]
    shown = [
        spec.name
        for spec in schema.columns
        if spec.name in owned and spec.name not in sensitive and spec.name not in PROVENANCE_FIELDS
    ][: MAX_COLUMNS - 1]
    header = ["source_id", *shown, "issues"]
    rows = [
        [redact_text(record.source_id), *_cells(record, shown), str(len(record.issues))]
        for record in records[:MAX_ROWS]
    ]
    widths = [
        min(CELL_WIDTH, max(len(row[i]) for row in [header, *rows])) for i in range(len(header))
    ]
    lines = [_line(header, widths), _line(["-" * width for width in widths], widths)]
    lines += [_line(row, widths) for row in rows]
    hidden = sorted(sensitive & owned)
    if hidden:
        lines.append(f"  hidden, marked sensitive in the mapping: {', '.join(hidden)}")
    if len(records) > MAX_ROWS:
        lines.append(f"  ... and {len(records) - MAX_ROWS} more record(s)")
    return lines


def _cells(record: Record, names: Sequence[str]) -> list[str]:
    values = record.to_dict()
    return [_cut(redact_text(_render(values[name]))) for name in names]


def _line(cells: Sequence[str], widths: Sequence[int]) -> str:
    padded = "  ".join(_cut(cell).ljust(width) for cell, width in zip(cells, widths, strict=True))
    return ("  " + padded).rstrip()


def _cut(text: str) -> str:
    return text if len(text) <= CELL_WIDTH else text[: CELL_WIDTH - len(_ELLIPSIS)] + _ELLIPSIS


def _render(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, (list, tuple)):
        return _render_list(value)
    if isinstance(value, dict):
        return _render_object(value)
    return str(value)


def _render_list(items: Sequence[object]) -> str:
    if not items:
        return "-"
    if all(isinstance(item, str) for item in items):
        return ", ".join(str(item) for item in items)
    if all(isinstance(item, dict) and "role" in item for item in items):
        return f"{len(items)} image(s)"
    if all(isinstance(item, dict) and "raw" in item for item in items):
        return ", ".join(str(item["raw"]) for item in items if isinstance(item, dict))
    return json.dumps(items, separators=(",", ":"), ensure_ascii=False)


def _render_object(value: dict[str, object]) -> str:
    if set(value) == {"amount", "currency"}:
        try:
            amount = Decimal(str(value["amount"]))
        except InvalidOperation:
            return json.dumps(value, separators=(",", ":"))
        return f"{amount:f} {value['currency']}"
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
