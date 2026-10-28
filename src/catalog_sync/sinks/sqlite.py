"""SQLite sink: one table keyed by (source, source_id), written with ``INSERT ... ON CONFLICT``.

Why not ``INSERT OR REPLACE``: REPLACE is delete-then-insert, so every foreign key declared
``ON DELETE CASCADE`` would wipe the child rows of each row being refreshed. An upsert changes the
row in place and writes only the columns the batch owns.

Schema handling is additive and automatic. The table is created on first use; an owned field with
no column yet gets one (``ALTER TABLE ... ADD COLUMN``). Nothing is ever dropped or retyped, and
columns the sink does not know (a stock column added by hand) are left alone. Scalars use native
column types; lists and objects are JSON text in a column declared ``JSON_TEXT``, which is how a
read-back knows to decode it.

The whole batch is one transaction, and a dry run is the same code run against an in-memory copy
of the database, so its report is what a real load would say and the real file is never opened for
writing.

One load at a time. ``backup()`` takes SQLite's write lock (``BEGIN IMMEDIATE``) and ``upsert()``
commits under it, so the backup is a copy of exactly what the load then changes: a second load
into the same file (another profile, another process) waits in its own ``backup()`` until the first
has committed, and its backup includes what the first wrote. A load that cannot get the lock within
``lock_wait_seconds`` fails with a ``SinkError`` and has written nothing. Readers are not blocked.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from catalog_sync.connector_info import SettingKind, SettingSpec
from catalog_sync.errors import ConfigError, SinkError
from catalog_sync.models import JsonObject, RecordKey
from catalog_sync.schema import FieldKind
from catalog_sync.sinks.base import BackupReceipt, Batch, WriteReport, row_key
from catalog_sync.sinks.lifecycle import SinkLifecycle, utc_now
from catalog_sync.sinks.sqlnames import IDENTIFIER, plain_identifier

DEFAULT_TABLE = "materials"
JSON_COLUMN_TYPE = "JSON_TEXT"
LOCK_WAIT_SECONDS = 5.0  # how long a load waits for another load into the same file to commit
PATH_SETTING = SettingSpec(
    "path",
    SettingKind.TEXT,
    "The SQLite database file; created if it does not exist. {workspace}/ stands for the "
    "workspace home, outside the checkout; any other relative path is relative to the config file.",
    default="{workspace}/out/catalog.db",
    essential=True,
)
TABLE_SETTING = SettingSpec(
    "table",
    SettingKind.TEXT,
    "The table the records go into (letters, digits and underscores).",
    default=DEFAULT_TABLE,
    essential=True,
)
SETTINGS = (PATH_SETTING, TABLE_SETTING)
_DECLARED_TYPES: Mapping[FieldKind, str] = {
    FieldKind.TEXT: "TEXT",
    FieldKind.INTEGER: "INTEGER",
    FieldKind.REAL: "REAL",
    FieldKind.JSON: JSON_COLUMN_TYPE,
    FieldKind.TIMESTAMP: "TEXT",  # no time type; parse_timestamp gives UTC, so text sorts by time
}


class SqliteSink(SinkLifecycle):
    def __init__(
        self,
        path: Path,
        *,
        table: str = DEFAULT_TABLE,
        clock: Callable[[], datetime] = utc_now,
        lock_wait_seconds: float = LOCK_WAIT_SECONDS,
    ) -> None:
        super().__init__(path, clock)
        self.table = plain_identifier(table, "table name")
        self._lock_wait_seconds = lock_wait_seconds
        self._conn: sqlite3.Connection | None = None

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> SqliteSink:
        unknown = set(options) - {spec.key for spec in SETTINGS}
        if unknown:
            raise ConfigError(f"sqlite sink: unknown option(s) {sorted(unknown)}")
        raw_path = options.get("path")
        if not isinstance(raw_path, str):
            raise ConfigError("sqlite sink needs a string option 'path'")
        table = options.get("table", DEFAULT_TABLE)
        if not isinstance(table, str) or not IDENTIFIER.fullmatch(table):
            raise ConfigError(f"sqlite sink: 'table' must match {IDENTIFIER.pattern}")
        return cls(base_dir / Path(raw_path).expanduser(), table=table)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        super().close()

    def backup(self) -> BackupReceipt:
        self._mark_backed_up()
        conn = self._connection()  # creates the file if need be: there must be something to lock
        _hold_write_lock(conn, self.path.name)  # released by the upsert's commit, or by close()
        try:
            return self._copy_database(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _copy_database(self, conn: sqlite3.Connection) -> BackupReceipt:
        if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0:
            return BackupReceipt(path=None, rows_before=0)
        rows_before = _count_rows(conn, self.table)
        # The online backup API copies a consistent snapshot of a database that may be in use;
        # copying the file itself could catch it mid-write. It runs on a second, read-only
        # connection: the one holding the write lock cannot be the source of its own backup (the
        # driver retries on "locked" forever), and readers are let in alongside that lock.
        backup_path = self._backup_path()
        _create_private_file(backup_path)
        target = sqlite3.connect(backup_path)
        try:
            with _read_only(self.path) as reader:
                reader.backup(target)
        finally:
            target.close()
        return BackupReceipt(path=backup_path, rows_before=rows_before)

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> WriteReport:
        self._require_backup_unless(is_dry_run=dry_run)
        if dry_run:
            return self._dry_run(batch)
        return _apply(self._connection(), self.table, batch, rollback=False, name=self.path.name)

    # --- connections ------------------------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                _create_private_file(self.path)
            # Autocommit mode: transactions are explicit (BEGIN/COMMIT), so DDL is part of them.
            self._conn = sqlite3.connect(
                self.path, isolation_level=None, timeout=self._lock_wait_seconds
            )
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def _dry_run(self, batch: Batch) -> WriteReport:
        scratch = sqlite3.connect(":memory:", isolation_level=None)
        try:
            if self.path.exists():
                with _read_only(self.path) as live:
                    live.backup(scratch)
            return _apply(scratch, self.table, batch, rollback=True, name=self.path.name)
        finally:
            scratch.close()


def read_table(path: Path, table: str = DEFAULT_TABLE) -> dict[RecordKey, JsonObject]:
    """Every row of `table` as JSON values; JSON_TEXT columns are decoded, NULL stays None."""
    with _read_only(path) as conn:
        conn.row_factory = sqlite3.Row
        columns = _column_types(conn, table)
        if not columns:
            return {}
        rows: dict[RecordKey, JsonObject] = {}
        for stored in conn.execute(f'SELECT * FROM "{plain_identifier(table, "table name")}"'):
            row = {name: _decode(stored[name], declared) for name, declared in columns.items()}
            rows[(str(row["source"]), str(row["source_id"]))] = row
    return rows


# --- applying a batch ---------------------------------------------------------------------------


def _hold_write_lock(conn: sqlite3.Connection, name: str) -> None:
    """Begin the write transaction unless this connection already holds it (from backup())."""
    if conn.in_transaction:
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise SinkError(
            f"{name} is locked by another writer ({exc}); another load into it may be running. "
            "Nothing was written: wait for it to finish, then retry"
        ) from exc


def _apply(
    conn: sqlite3.Connection, table: str, batch: Batch, *, rollback: bool, name: str
) -> WriteReport:
    _hold_write_lock(conn, name)
    try:
        report = apply_in_transaction(conn, table, batch)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if rollback else "COMMIT")
    return report


def apply_in_transaction(conn: sqlite3.Connection, table: str, batch: Batch) -> WriteReport:
    """Create or extend `table` and upsert the batch's rows, inside the caller's transaction.

    The caller owns BEGIN and COMMIT, which is what lets it write other things atomically with
    the rows (the reference platform receiver records the request that carried them).
    """
    for name in batch.owned:
        plain_identifier(name, "column name")  # names are spliced into SQL, so only plain ones pass
    _ensure_columns(conn, table, batch)
    return _upsert_rows(conn, table, batch)


def _ensure_columns(conn: sqlite3.Connection, table: str, batch: Batch) -> None:
    quoted_table = f'"{table}"'
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {quoted_table} "
        '("source" TEXT NOT NULL, "source_id" TEXT NOT NULL, PRIMARY KEY ("source", "source_id"))'
    )
    known = _column_types(conn, table)
    for name in sorted(batch.owned):
        if name not in known:
            declared = _DECLARED_TYPES[batch.kinds[name]]
            conn.execute(f'ALTER TABLE {quoted_table} ADD COLUMN "{name}" {declared}')


def _upsert_rows(conn: sqlite3.Connection, table: str, batch: Batch) -> WriteReport:
    statement = upsert_statement(table, batch)
    names = sorted(batch.owned)
    inserted = updated = unchanged = 0
    for row in batch.rows:
        key = row_key(row)
        existed = conn.execute(
            f'SELECT 1 FROM "{table}" WHERE "source" = ? AND "source_id" = ?', key
        ).fetchone()
        values = [*key, *(_encode(row[name], batch.kinds[name]) for name in names)]
        changed = conn.execute(statement, values).rowcount
        if existed is None:
            inserted += 1
        elif changed:
            updated += 1
        else:
            unchanged += 1
    return WriteReport(inserted, updated, unchanged, _absent_keys(conn, table, batch))


def upsert_statement(table: str, batch: Batch) -> str:
    """The one INSERT ... ON CONFLICT ... DO UPDATE every row of the batch is run through.

    The WHERE clause makes an update that would change nothing a no-op, which is what lets the
    row count tell "updated" from "unchanged" without a second pass.
    """
    names = sorted(batch.owned)
    quoted = [f'"{name}"' for name in names]
    columns = ", ".join(['"source"', '"source_id"', *quoted])
    marks = ", ".join("?" for _ in range(len(names) + 2))
    head = f'INSERT INTO "{table}" ({columns}) VALUES ({marks}) ON CONFLICT ("source", "source_id")'
    if not names:
        return f"{head} DO NOTHING"
    new_values = {name: _new_value(table, name, batch) for name in names}
    assignments = ", ".join(f'"{name}" = {new_values[name]}' for name in names)
    differs = " OR ".join(f'{new_values[name]} IS NOT "{table}"."{name}"' for name in names)
    return f"{head} DO UPDATE SET {assignments} WHERE {differs}"


def _new_value(table: str, name: str, batch: Batch) -> str:
    incoming, stored = f'excluded."{name}"', f'"{table}"."{name}"'
    if name not in batch.keep_existing_if_null:
        return incoming
    if batch.kinds[name] is not FieldKind.JSON:
        return f"COALESCE({incoming}, {stored})"
    # An empty list or object is the JSON spelling of "nothing", as NULL is for scalars.
    return (
        f"CASE WHEN ({incoming} IS NULL OR {incoming} IN ('[]', '{{}}')) "
        f"AND ({stored} IS NOT NULL AND {stored} NOT IN ('[]', '{{}}')) "
        f"THEN {stored} ELSE {incoming} END"
    )


def _absent_keys(conn: sqlite3.Connection, table: str, batch: Batch) -> tuple[RecordKey, ...]:
    if not batch.is_full_snapshot:
        return ()
    seen = {row_key(row) for row in batch.rows}
    sources = sorted({key[0] for key in seen})
    if not sources:
        return ()
    marks = ", ".join("?" for _ in sources)
    stored = conn.execute(
        f'SELECT "source", "source_id" FROM "{table}" WHERE "source" IN ({marks}) ORDER BY rowid',
        sources,
    )
    return tuple(key for key in ((row[0], row[1]) for row in stored) if key not in seen)


# --- encoding and small helpers -----------------------------------------------------------------


def _encode(value: object, kind: FieldKind) -> object:
    if value is None:
        return None
    if kind is FieldKind.JSON:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    if kind is FieldKind.REAL and isinstance(value, (int, float)):
        return float(value)
    return value


def _decode(value: object, declared_type: str) -> object:
    if value is None or declared_type != JSON_COLUMN_TYPE:
        return value
    if not isinstance(value, str):
        raise SinkError(f"a {JSON_COLUMN_TYPE} cell holds {type(value).__name__}, not text")
    return json.loads(value)


def _column_types(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    info = conn.execute(f'PRAGMA table_info("{plain_identifier(table, "table name")}")')
    return {row[1]: row[2] for row in info}


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    if not _column_types(conn, table):
        return 0
    return int(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def _create_private_file(path: Path) -> None:
    """An empty file only the owner can read: SQLite fills in an existing empty file as it is."""
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))


@contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        yield conn
