"""Postgres sink: one table keyed by (source, source_id), written with ``INSERT ... ON CONFLICT``.

Same contract as the SQLite sink (see ``catalog_sync.sinks.base``), with the Postgres idioms:

* A row is changed in place by ``INSERT ... ON CONFLICT (source, source_id) DO UPDATE`` that sets
  only the columns the batch owns, so foreign keys declared ``ON DELETE CASCADE`` never fire on a
  refresh (``INSERT ... ON CONFLICT`` does not delete; ``DELETE`` then ``INSERT`` would).
  ``keep_existing_if_null`` becomes ``COALESCE`` for scalars and a ``CASE`` for jsonb, where ``[]``
  and ``{}`` count as "nothing".
* Column types follow the field kind: text, bigint, double precision, jsonb, timestamptz. A
  timestamptz comes back in UTC: the instant is kept, the offset it was written with is not.
* Schema changes are additive. The table is created on first use and an owned field with no column
  gets one (``ALTER TABLE ... ADD COLUMN``); nothing is dropped or retyped, and columns this sink
  does not know are left alone.
* The whole batch is one transaction. A dry run is the same code in a transaction that is rolled
  back, so its report is what a real load would say and nothing, not even a new column, remains.

Backup. ``backup()`` is called before every load, but a load does not touch the whole table, so
copying the whole table each time would grow without bound on a large one. Instead the first
``upsert()`` that follows copies the *current* version of every row it is about to change into
``<table>__backup_<timestamp>`` (same schema, same primary key) inside the same transaction as
the write. The backup therefore holds exactly the pre-load state of the affected rows, or nothing
if the load fails. Rows the load inserts have no earlier state and are not in it. To undo a load,
copy the rows back (``INSERT ... ON CONFLICT DO UPDATE``) and delete the rows the load inserted.
Loads into one table are serialised: every real ``upsert()`` takes a transaction-scoped advisory
lock on the table's name first (``pg_advisory_xact_lock``), so a second load waits until the first
has committed. Without it, under READ COMMITTED the second load's pre-image copy could run while
the first load's rows were uncommitted, and the "pre-load" backup would miss them. The lock is
released by the commit or rollback and has no timeout: a load queued behind another waits for it.
This is a safety net for one load, not a replacement for ``pg_dump`` or point-in-time recovery:
whole-database backup is a deployment concern and out of scope here. Backup tables are never
deleted by this tool; drop them when they have served their purpose. They hold the same data as
the table, so they need the same access controls.

The connection string is read from an environment variable named in the sink options
(``dsn_env``), never from the config file, so a password does not end up in a file that is
checked in. The ``psycopg`` driver is an optional extra: ``pip install 'catalog-sync[postgres]'``.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from catalog_sync.connector_info import SettingKind, SettingSpec
from catalog_sync.errors import ConfigError, SinkError
from catalog_sync.models import JsonObject, RecordKey
from catalog_sync.schema import FieldKind
from catalog_sync.sinks.base import BackupReceipt, Batch, Row, WriteReport, row_key
from catalog_sync.sinks.lifecycle import SinkState, utc_now
from catalog_sync.sinks.sqlnames import IDENTIFIER, plain_identifier

if TYPE_CHECKING:
    import psycopg

DEFAULT_TABLE = "materials"
DSN_ENV_SETTING = SettingSpec(
    "dsn_env",
    SettingKind.ENV_NAME,
    "The NAME of the environment variable that holds the connection string (never the string, "
    "which may carry a password).",
    required=True,
)
TABLE_SETTING = SettingSpec(
    "table",
    SettingKind.TEXT,
    "The table the records go into (letters, digits and underscores).",
    default=DEFAULT_TABLE,
    essential=True,
)
SCHEMA_SETTING = SettingSpec(
    "schema", SettingKind.TEXT, "The Postgres schema the table lives in; the default if empty."
)
SETTINGS = (DSN_ENV_SETTING, TABLE_SETTING, SCHEMA_SETTING)
APPLICATION_NAME = "catalog-sync"
# Postgres cuts identifiers at 63 bytes without a word, which could make two backup tables
# collide, so the table name is limited to leave room for the suffix.
_MAX_IDENTIFIER = 63
_BACKUP_INFIX = "__backup_"
_BACKUP_STAMP = "%Y%m%dT%H%M%S%f"
_BACKUP_SUFFIX_LENGTH = len(_BACKUP_INFIX) + len(datetime(2000, 1, 1).strftime(_BACKUP_STAMP))
_PIPELINE_CHUNK = 500  # statements sent per round trip; keeps a large load from one-by-one latency
_DECLARED_TYPES: Mapping[FieldKind, str] = {
    FieldKind.TEXT: "text",
    FieldKind.INTEGER: "bigint",
    FieldKind.REAL: "double precision",
    FieldKind.JSON: "jsonb",
    FieldKind.TIMESTAMP: "timestamptz",
}
# How a bound parameter is converted on the server; strings and numbers go through unchanged.
_PARAMETER_CASTS: Mapping[FieldKind, str] = {
    FieldKind.JSON: "::jsonb",
    FieldKind.TIMESTAMP: "::timestamptz",
}


class PostgresSink(SinkState):
    def __init__(
        self,
        dsn_env: str,
        *,
        table: str = DEFAULT_TABLE,
        schema: str | None = None,
        clock: Callable[[], datetime] = utc_now,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(clock)
        self.dsn_env = dsn_env
        self.table = plain_identifier(table, "table name")
        if len(table) + _BACKUP_SUFFIX_LENGTH > _MAX_IDENTIFIER:
            limit = _MAX_IDENTIFIER - _BACKUP_SUFFIX_LENGTH
            raise ConfigError(f"postgres sink: 'table' may have at most {limit} characters")
        self.schema = None if schema is None else plain_identifier(schema, "schema name")
        self._environ = os.environ if environ is None else environ
        self._conn: psycopg.Connection | None = None
        self._backup_table: str | None = None
        # Keys this load created. They have no pre-load state, so a later upsert of the same
        # load must not copy them into the backup as if they had.
        self._created_keys: set[RecordKey] = set()

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> PostgresSink:
        if "dsn" in options:
            raise ConfigError(
                "postgres sink: there is no 'dsn' option, because a connection string may carry "
                "a password and a config file is a place for it to leak from. Put the string in "
                "an environment variable and name that variable in 'dsn_env'"
            )
        unknown = set(options) - {spec.key for spec in SETTINGS}
        if unknown:
            raise ConfigError(f"postgres sink: unknown option(s) {sorted(unknown)}")
        dsn_env = options.get("dsn_env")
        if not isinstance(dsn_env, str) or not dsn_env:
            raise ConfigError(
                "postgres sink needs 'dsn_env': the name of the environment variable that holds "
                "the connection string (the string itself, which may carry a password, does not "
                "belong in the config file)"
            )
        table = options.get("table", DEFAULT_TABLE)
        schema = options.get("schema")
        if not isinstance(table, str) or not IDENTIFIER.fullmatch(table):
            raise ConfigError(f"postgres sink: 'table' must match {IDENTIFIER.pattern}")
        if schema is not None and (not isinstance(schema, str) or not IDENTIFIER.fullmatch(schema)):
            raise ConfigError(f"postgres sink: 'schema' must match {IDENTIFIER.pattern}")
        return cls(dsn_env, table=table, schema=schema)

    # --- lifecycle --------------------------------------------------------------------------

    def open(self) -> None:
        self._conn = connect(self._dsn())  # first: a failed connection leaves the sink closed
        super().open()
        self._backup_table = None
        self._created_keys = set()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        super().close()

    def backup(self) -> BackupReceipt:
        self._mark_backed_up()
        conn = self._connection()
        # "Before the load" means before this backup: rows an earlier upsert of the session created
        # exist now and have a state worth keeping.
        self._backup_table = None
        self._created_keys = set()
        target = qualified_name(self.schema, self.table)
        if not _exists(conn, target):
            return BackupReceipt(path=None, rows_before=0)
        [(rows_before,)] = conn.execute(f"SELECT count(*) FROM {target}").fetchall()
        name = f"{self.table}{_BACKUP_INFIX}{self._clock().strftime(_BACKUP_STAMP)}"
        backup_table = qualified_name(self.schema, name)
        if _exists(conn, backup_table):
            raise SinkError(f"backup table {name} already exists; not overwriting it")
        driver = require_driver()
        try:
            with conn.transaction():
                # LIKE copies the columns and their NOT NULLs, not defaults, indexes or foreign
                # keys: a backup is data, and must not cascade or fire anything.
                conn.execute(f"CREATE TABLE {backup_table} (LIKE {target})")
                conn.execute(f'ALTER TABLE {backup_table} ADD PRIMARY KEY ("source", "source_id")')
        except driver.Error as exc:
            raise SinkError(f"cannot create backup table {name}: {_describe(exc)}") from exc
        self._backup_table = name
        return BackupReceipt(path=None, rows_before=rows_before, table=name)

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> WriteReport:
        self._require_backup_unless(is_dry_run=dry_run)
        for name in batch.owned:
            plain_identifier(name, "column name")  # names are spliced into SQL
        conn = self._connection()
        driver = require_driver()
        try:
            with conn.transaction():
                report, created = self._apply(conn, batch, copy_pre_images=not dry_run)
                if dry_run:
                    raise driver.Rollback  # the report is complete; undo everything it did
        except driver.Error as exc:
            raise SinkError(f"postgres rejected the load and undid it: {_describe(exc)}") from exc
        if not dry_run:
            self._created_keys |= created
        return report

    # --- one transaction --------------------------------------------------------------------

    def _apply(
        self, conn: psycopg.Connection, batch: Batch, *, copy_pre_images: bool
    ) -> tuple[WriteReport, set[RecordKey]]:
        target = qualified_name(self.schema, self.table)
        if copy_pre_images:
            _lock_table_for_load(conn, target)
        if copy_pre_images and self._backup_table is not None:
            keys = [row_key(row) for row in batch.rows if row_key(row) not in self._created_keys]
            self._copy_pre_images(conn, target, keys)
        _ensure_columns(conn, target, batch)
        counts, created = _upsert_rows(conn, upsert_statement(self.table, target, batch), batch)
        absent = _absent_keys(conn, target, batch)
        return WriteReport(counts[0], counts[1], counts[2], absent), created

    def _copy_pre_images(
        self, conn: psycopg.Connection, target: str, keys: Sequence[RecordKey]
    ) -> None:
        """Copy the current version of the rows about to change, once per load."""
        if not keys or self._backup_table is None:
            return
        backup_table = qualified_name(self.schema, self._backup_table)
        columns = ", ".join(_quote(name) for name in _columns(conn, backup_table))
        conn.execute(
            f"INSERT INTO {backup_table} ({columns}) SELECT {columns} FROM {target} "
            'WHERE ("source", "source_id") IN '
            "(SELECT k.source, k.source_id "
            "FROM unnest(%s::text[], %s::text[]) AS k(source, source_id)) "
            'ON CONFLICT ("source", "source_id") DO NOTHING',
            ([key[0] for key in keys], [key[1] for key in keys]),
        )

    # --- connection -------------------------------------------------------------------------

    def _dsn(self) -> str:
        dsn = self._environ.get(self.dsn_env)
        if not dsn:
            raise ConfigError(
                f"postgres sink: the environment variable {self.dsn_env} is not set; it must "
                "hold the connection string"
            )
        return dsn

    def _connection(self) -> psycopg.Connection:
        self._require_open()
        if self._conn is None:
            raise AssertionError("open() sets the connection")
        return self._conn


def connect(dsn: str) -> psycopg.Connection:
    """An autocommit connection in UTC; transactions are explicit."""
    driver = require_driver()
    try:
        conn: psycopg.Connection = driver.connect(
            dsn, autocommit=True, application_name=APPLICATION_NAME
        )
    except driver.OperationalError as exc:
        raise SinkError(f"cannot connect to Postgres: {_describe(exc)}") from exc
    except driver.ProgrammingError as exc:
        # The driver's message can quote part of the string it could not parse, which may be a
        # password; say only that the string is unusable.
        raise SinkError("the Postgres connection string is not valid") from exc
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


def require_driver() -> ModuleType:
    try:
        return importlib.import_module("psycopg")
    except ImportError as exc:
        raise ConfigError(
            "the postgres sink needs the optional driver: pip install 'catalog-sync[postgres]'"
        ) from exc


def read_table(
    dsn: str, table: str = DEFAULT_TABLE, schema: str | None = None
) -> dict[RecordKey, JsonObject]:
    """Every row of `table` as JSON values: timestamps as ISO strings in UTC, NULL as None."""
    plain_identifier(table, "table name")
    name = qualified_name(schema, table)
    conn = connect(dsn)
    try:
        if not _exists(conn, name):
            return {}
        cursor = conn.execute(f"SELECT * FROM {name}")
        names = [column.name for column in cursor.description or ()]
        rows: dict[RecordKey, JsonObject] = {}
        for values in cursor.fetchall():
            row = {column: _as_json(value) for column, value in zip(names, values, strict=True)}
            rows[(str(row["source"]), str(row["source_id"]))] = row
    finally:
        conn.close()
    return rows


# --- statements -------------------------------------------------------------------------------


def qualified_name(schema: str | None, table: str) -> str:
    quoted = _quote(table)
    return quoted if schema is None else f"{_quote(schema)}.{quoted}"


def upsert_statement(table: str, target: str, batch: Batch) -> str:
    """The one INSERT ... ON CONFLICT ... DO UPDATE every row of the batch is run through.

    The WHERE clause makes an update that would change nothing a no-op, which is what lets the
    result tell "updated" from "unchanged". ``xmax = 0`` is true only for a tuple this very
    statement inserted, which is how an insert is told from an update without a second query.
    """
    names = sorted(batch.owned)
    columns = ", ".join(_quote(name) for name in ["source", "source_id", *names])
    marks = ", ".join(
        ["%s", "%s", *(f"%s{_PARAMETER_CASTS.get(batch.kinds[name], '')}" for name in names)]
    )
    head = f'INSERT INTO {target} ({columns}) VALUES ({marks}) ON CONFLICT ("source", "source_id")'
    tail = "RETURNING (xmax = 0)"
    if not names:
        return f"{head} DO NOTHING {tail}"
    new_values = {name: _new_value(table, name, batch) for name in names}
    assignments = ", ".join(f"{_quote(name)} = {new_values[name]}" for name in names)
    differs = " OR ".join(
        f"{new_values[name]} IS DISTINCT FROM {_quote(table)}.{_quote(name)}" for name in names
    )
    return f"{head} DO UPDATE SET {assignments} WHERE {differs} {tail}"


def _new_value(table: str, name: str, batch: Batch) -> str:
    incoming, stored = f"excluded.{_quote(name)}", f"{_quote(table)}.{_quote(name)}"
    if name not in batch.keep_existing_if_null:
        return incoming
    if batch.kinds[name] is not FieldKind.JSON:
        return f"COALESCE({incoming}, {stored})"
    # An empty list or object is the JSON spelling of "nothing", as NULL is for scalars.
    nothing = "('[]'::jsonb, '{}'::jsonb)"
    return (
        f"CASE WHEN ({incoming} IS NULL OR {incoming} IN {nothing}) "
        f"AND ({stored} IS NOT NULL AND {stored} NOT IN {nothing}) "
        f"THEN {stored} ELSE {incoming} END"
    )


def _lock_table_for_load(conn: psycopg.Connection, target: str) -> None:
    """Wait for any other load into `target` to finish, and keep others out until this commits.

    The key is a hash of the quoted, schema-qualified name as this sink spells it, so two sinks
    that reach one table under different spellings (no schema versus ``public``) do not exclude
    each other: name the schema in every profile that loads into a shared table.
    """
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"catalog-sync:{target}",)
    )


def _ensure_columns(conn: psycopg.Connection, target: str, batch: Batch) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {target} "
        '("source" text NOT NULL, "source_id" text NOT NULL, PRIMARY KEY ("source", "source_id"))'
    )
    known = set(_columns(conn, target))
    for name in sorted(batch.owned):
        if name not in known:
            conn.execute(f"ALTER TABLE {target} ADD COLUMN {_quote(name)} {_declared(batch, name)}")


def _declared(batch: Batch, name: str) -> str:
    return _DECLARED_TYPES[batch.kinds[name]]


def _upsert_rows(
    conn: psycopg.Connection, statement: str, batch: Batch
) -> tuple[tuple[int, int, int], set[RecordKey]]:
    names = sorted(batch.owned)
    inserted = updated = unchanged = 0
    created: set[RecordKey] = set()
    for start in range(0, len(batch.rows), _PIPELINE_CHUNK):
        chunk = batch.rows[start : start + _PIPELINE_CHUNK]
        with conn.pipeline():
            cursors = [conn.execute(statement, _parameters(row, batch, names)) for row in chunk]
        for row, cursor in zip(chunk, cursors, strict=True):
            outcome = cursor.fetchone()
            if outcome is None:
                unchanged += 1
            elif outcome[0]:
                inserted += 1
                created.add(row_key(row))
            else:
                updated += 1
    return (inserted, updated, unchanged), created


def _parameters(row: Row, batch: Batch, names: Sequence[str]) -> list[object]:
    return [*row_key(row), *(_encode(row[name], batch.kinds[name]) for name in names)]


def _absent_keys(conn: psycopg.Connection, target: str, batch: Batch) -> tuple[RecordKey, ...]:
    if not batch.is_full_snapshot:
        return ()
    seen = {row_key(row) for row in batch.rows}
    sources = sorted({key[0] for key in seen})
    if not sources:
        return ()
    stored = conn.execute(
        f'SELECT "source", "source_id" FROM {target} WHERE "source" = ANY(%s) '
        'ORDER BY "source", "source_id"',
        (sources,),
    ).fetchall()
    return tuple(key for key in ((row[0], row[1]) for row in stored) if key not in seen)


# --- catalog queries and small helpers --------------------------------------------------------


def _exists(conn: psycopg.Connection, qualified: str) -> bool:
    [(found,)] = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,)).fetchall()
    return bool(found)


def _columns(conn: psycopg.Connection, qualified: str) -> list[str]:
    """Column names of a table, found through the search path like any query would."""
    rows = conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
        (qualified,),
    ).fetchall()
    return [row[0] for row in rows]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _encode(value: object, kind: FieldKind) -> object:
    if value is None:
        return None
    if kind is FieldKind.JSON:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if kind is FieldKind.REAL and isinstance(value, (int, float)):
        return float(value)
    return value


def _as_json(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def _describe(exc: BaseException) -> str:
    """The server's own message, first line only, without the statement or its parameters."""
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None)
    text = primary if isinstance(primary, str) and primary else str(exc)
    return f"{type(exc).__name__}: {text.splitlines()[0] if text else ''}"
