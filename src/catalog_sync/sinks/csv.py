"""CSV sink: one row per record, merged into the existing file by (source, source_id).

Everything in a CSV is text, so the sink works on cells and never needs to decode a column it does
not own: unowned and unknown columns pass through byte for byte. Encoding of an owned value:

* null is an empty cell (so an empty string cannot be told from null; that is the format's limit);
* numbers are written in their shortest round-trip form;
* lists and objects are compact JSON text with sorted keys, so equal data gives equal cells and an
  unchanged row is recognised as unchanged;
* text that a spreadsheet would run as a formula is neutralised (see below).

Formula injection: Excel, LibreOffice and Sheets execute a cell that starts with ``=``, ``+``,
``-``, ``@``, a tab or a carriage return when the file is opened. Text comes from a remote service
and may be hostile, so a text cell that starts like that (after any run of apostrophes) is written
with one apostrophe in front, the escape the spreadsheet world already uses. Reading undoes exactly
that, so every value round-trips through this sink, including one that already starts with an
apostrophe. Other readers of the file see the apostrophe. Numbers are exempt: ``-5`` is a number,
not a formula, and only text cells can carry a payload. The key columns are text too. Cells of
columns this sink does not own are left alone, so a file written by an older version is fixed the
next time its cells are rewritten.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from catalog_sync.connector_info import SettingKind, SettingSpec
from catalog_sync.errors import ConfigError, RecordFormatError
from catalog_sync.fsutil import atomic_write_bytes
from catalog_sync.models import JsonObject, RecordKey
from catalog_sync.schema import FieldKind
from catalog_sync.sinks.base import BackupReceipt, Batch, Row, WriteReport, row_key
from catalog_sync.sinks.lifecycle import LockedFileSink, utc_now

_KEY_COLUMNS = ("source", "source_id")
_EMPTY_JSON_CELLS = frozenset({"[]", "{}"})
# A cell is dangerous when it starts with one of these; leading apostrophes are skipped so that
# "'=1" (a value that already carries one) is told apart from "=1" and the pair stays reversible.
_FORMULA_START = re.compile(r"'*[=+\-@\t\r]")
_TEXT_KINDS = (FieldKind.TEXT, FieldKind.TIMESTAMP)

Cells = dict[str, str]

PATH_SETTING = SettingSpec(
    "path",
    SettingKind.TEXT,
    "The CSV file to write. {workspace}/ stands for the workspace home, outside the "
    "checkout; any other relative path is relative to the config file.",
    default="{workspace}/out/catalog.csv",
    essential=True,
)
SETTINGS = (PATH_SETTING,)


class CsvSink(LockedFileSink):
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(path, clock)

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> CsvSink:
        unknown = set(options) - {PATH_SETTING.key}
        if unknown:
            raise ConfigError(f"csv sink: unknown option(s) {sorted(unknown)}")
        raw_path = options.get("path")
        if not isinstance(raw_path, str):
            raise ConfigError("csv sink needs a string option 'path'")
        return cls(base_dir / Path(raw_path).expanduser())

    def backup(self) -> BackupReceipt:
        self._mark_backed_up()
        if not self.path.exists():
            return BackupReceipt(path=None, rows_before=0)
        rows_before = len(self._read_table()[1])  # also proves the file is readable
        return BackupReceipt(path=self._copy_file_backup(), rows_before=rows_before)

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> WriteReport:
        self._require_backup_unless(is_dry_run=dry_run)
        header, rows = self._read_table()
        absent = _absent_keys(rows, batch)
        columns = _extend_header(header, batch)
        inserted = updated = unchanged = 0
        for incoming in batch.rows:
            key = row_key(incoming)
            before = rows.get(key)
            if before is None:
                rows[key] = _merge({"source": key[0], "source_id": key[1]}, incoming, batch)
                inserted += 1
                continue
            merged = _merge(before, incoming, batch)
            if _same_cells(merged, before):
                unchanged += 1
            else:
                rows[key] = merged
                updated += 1
        # A new owned column alone also rewrites an existing file; with no file there is nothing
        # to write unless a row was inserted.
        is_changed = bool(inserted or updated) or (bool(header) and columns != header)
        if is_changed and not dry_run:
            self._write_table(columns, rows)
        return WriteReport(inserted, updated, unchanged, absent)

    def read_rows(self, kinds: Mapping[str, FieldKind]) -> dict[RecordKey, JsonObject]:
        """The stored rows, each cell decoded as its kind (a column without a kind stays text)."""
        _, rows = self._read_table()
        return {
            key: {
                name: decode_cell(cell, kinds.get(name, FieldKind.TEXT))
                for name, cell in cells.items()
            }
            for key, cells in rows.items()
        }

    # --- file access ------------------------------------------------------------------------

    def _read_table(self) -> tuple[list[str], dict[RecordKey, Cells]]:
        if not self.path.exists():
            return [], {}
        with self.path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None:
                raise RecordFormatError(f"{self.path.name} is empty: no header row")
            _check_header(header, self.path.name)
            rows: dict[RecordKey, Cells] = {}
            for number, cells in enumerate(reader, start=2):
                if not cells:
                    continue
                key, row = _parse_row(header, cells, number, self.path.name)
                if key in rows:
                    raise RecordFormatError(f"{self.path.name} line {number}: duplicate key {key}")
                rows[key] = row
        return header, rows

    def _write_table(self, columns: list[str], rows: Mapping[RecordKey, Cells]) -> None:
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(columns)
        for (source, source_id), cells in rows.items():
            # The keys are text from a remote service as well; writing them from the decoded key
            # also repairs a key cell an older version left unguarded.
            shown = {**cells, "source": guard_cell(source), "source_id": guard_cell(source_id)}
            writer.writerow([shown.get(column, "") for column in columns])
        atomic_write_bytes(self.path, buffer.getvalue().encode("utf-8"))


def encode_cell(value: object, kind: FieldKind) -> str:
    if value is None:
        return ""
    if kind is FieldKind.JSON:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    if kind is FieldKind.REAL and isinstance(value, (int, float)):
        return repr(float(value))
    return guard_cell(str(value)) if kind in _TEXT_KINDS else str(value)


def decode_cell(cell: str, kind: FieldKind) -> object:
    if cell == "":
        return None
    if kind is FieldKind.JSON:
        return json.loads(cell)
    if kind is FieldKind.INTEGER:
        return int(cell)
    if kind is FieldKind.REAL:
        return float(cell)
    return unguard_cell(cell) if kind in _TEXT_KINDS else cell


def guard_cell(text: str) -> str:
    """`text` as a cell a spreadsheet will show instead of evaluate."""
    return "'" + text if _FORMULA_START.match(text) else text


def unguard_cell(cell: str) -> str:
    """The inverse of `guard_cell`."""
    return cell[1:] if cell.startswith("'") and _FORMULA_START.match(cell) else cell


def _is_empty_cell(cell: str, kind: FieldKind) -> bool:
    return cell == "" or (kind is FieldKind.JSON and cell in _EMPTY_JSON_CELLS)


def _merge(existing: Cells, incoming: Row, batch: Batch) -> Cells:
    merged = dict(existing)
    for name in sorted(batch.owned):
        kind = batch.kinds[name]
        cell = encode_cell(incoming[name], kind)
        keeps_old_value = (
            name in batch.keep_existing_if_null
            and _is_empty_cell(cell, kind)
            and not _is_empty_cell(existing.get(name, ""), kind)
        )
        if not keeps_old_value:
            merged[name] = cell
    return merged


def _same_cells(first: Cells, second: Cells) -> bool:
    """Equal as rows of a table: a column a row never had reads as an empty cell."""
    return all(first.get(name, "") == second.get(name, "") for name in first.keys() | second.keys())


def _extend_header(header: list[str], batch: Batch) -> list[str]:
    if not header:
        return [*_KEY_COLUMNS, *sorted(batch.owned)]
    return [*header, *(name for name in sorted(batch.owned) if name not in header)]


def _check_header(header: list[str], file_name: str) -> None:
    missing = [name for name in _KEY_COLUMNS if name not in header]
    if missing:
        raise RecordFormatError(f"{file_name} lacks the key column(s) {missing}")
    if len(set(header)) != len(header):
        raise RecordFormatError(f"{file_name} has duplicate column names")


def _parse_row(
    header: list[str], cells: list[str], number: int, file_name: str
) -> tuple[RecordKey, Cells]:
    if len(cells) > len(header):
        raise RecordFormatError(f"{file_name} line {number} has more cells than columns")
    row = dict(zip(header, [*cells, *[""] * (len(header) - len(cells))], strict=True))
    if not row["source"] or not row["source_id"]:
        raise RecordFormatError(f"{file_name} line {number} lacks 'source' and 'source_id'")
    return (unguard_cell(row["source"]), unguard_cell(row["source_id"])), row


def _absent_keys(rows: Mapping[RecordKey, Cells], batch: Batch) -> tuple[RecordKey, ...]:
    if not batch.is_full_snapshot:
        return ()
    seen = {row_key(row) for row in batch.rows}
    sources = {key[0] for key in seen}
    return tuple(key for key in rows if key[0] in sources and key not in seen)
