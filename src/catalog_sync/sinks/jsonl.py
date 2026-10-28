"""JSON Lines sink: one row per line, merged into the existing file by (source, source_id).

A flat file cannot do ``ON CONFLICT DO UPDATE``, so this sink reads the whole file, merges at the
JSON-object level and atomically rewrites it. Working on raw objects is what keeps columns it has
never heard of, such as a hand-added stock column, intact. A new row holds the key plus the fields
some batch owned, nothing else: the sink does not know what defaults another record type would
want.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from catalog_sync.connector_info import SettingKind, SettingSpec
from catalog_sync.errors import ConfigError, RecordFormatError
from catalog_sync.fsutil import atomic_write_bytes
from catalog_sync.models import JsonObject, RecordKey, is_empty_json
from catalog_sync.sinks.base import BackupReceipt, Batch, Row, WriteReport, row_key
from catalog_sync.sinks.lifecycle import LockedFileSink, utc_now

PATH_SETTING = SettingSpec(
    "path",
    SettingKind.TEXT,
    "The JSON Lines file to write. {workspace}/ stands for the workspace home, outside the "
    "checkout; any other relative path is relative to the config file.",
    default="{workspace}/out/catalog.jsonl",
    essential=True,
)
SETTINGS = (PATH_SETTING,)


class JsonlSink(LockedFileSink):
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(path, clock)

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> JsonlSink:
        unknown = set(options) - {PATH_SETTING.key}
        if unknown:
            raise ConfigError(f"jsonl sink: unknown option(s) {sorted(unknown)}")
        raw_path = options.get("path")
        if not isinstance(raw_path, str):
            raise ConfigError("jsonl sink needs a string option 'path'")
        return cls(base_dir / Path(raw_path).expanduser())

    def backup(self) -> BackupReceipt:
        self._mark_backed_up()
        if not self.path.exists():
            return BackupReceipt(path=None, rows_before=0)
        rows_before = len(self._read_rows())  # also proves the file is readable before we trust it
        return BackupReceipt(path=self._copy_file_backup(), rows_before=rows_before)

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> WriteReport:
        self._require_backup_unless(is_dry_run=dry_run)
        rows = self._read_rows()
        absent = _absent_keys(rows, batch)
        inserted = updated = unchanged = 0
        for incoming in batch.rows:
            key = row_key(incoming)
            before = rows.get(key)
            if before is None:
                rows[key] = _merge({"source": key[0], "source_id": key[1]}, incoming, batch)
                inserted += 1
                continue
            merged = _merge(before, incoming, batch)
            if merged == before:
                unchanged += 1
            else:
                rows[key] = merged
                updated += 1
        if (inserted or updated) and not dry_run:
            self._write_rows(rows)
        return WriteReport(inserted, updated, unchanged, absent)

    def _read_rows(self) -> dict[RecordKey, JsonObject]:
        rows: dict[RecordKey, JsonObject] = {}
        if not self.path.exists():
            return rows
        text = self.path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            row = _parse_row(line, number)
            key = (str(row["source"]), str(row["source_id"]))
            if key in rows:
                raise RecordFormatError(f"{self.path.name} line {number}: duplicate key {key}")
            rows[key] = row
        return rows

    def _write_rows(self, rows: Mapping[RecordKey, JsonObject]) -> None:
        lines = [json.dumps(row, ensure_ascii=False, allow_nan=False) for row in rows.values()]
        atomic_write_bytes(self.path, ("\n".join(lines) + "\n").encode("utf-8"))


def _parse_row(line: str, number: int) -> JsonObject:
    try:
        row = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RecordFormatError(f"line {number} is not valid JSON") from exc
    if not isinstance(row, dict):
        raise RecordFormatError(f"line {number} is not a JSON object")
    if not isinstance(row.get("source"), str) or not isinstance(row.get("source_id"), str):
        raise RecordFormatError(f"line {number} lacks string 'source' and 'source_id'")
    return row


def _merge(existing: JsonObject, incoming: Row, batch: Batch) -> JsonObject:
    merged = dict(existing)
    for name in sorted(batch.owned):
        value = incoming[name]
        keeps_old_value = (
            name in batch.keep_existing_if_null
            and is_empty_json(value)
            and not is_empty_json(existing.get(name))
        )
        if not keeps_old_value:
            merged[name] = value
    return merged


def _absent_keys(rows: Mapping[RecordKey, JsonObject], batch: Batch) -> tuple[RecordKey, ...]:
    if not batch.is_full_snapshot:
        return ()
    seen = {row_key(row) for row in batch.rows}
    sources = {key[0] for key in seen}
    return tuple(key for key in rows if key[0] in sources and key not in seen)
