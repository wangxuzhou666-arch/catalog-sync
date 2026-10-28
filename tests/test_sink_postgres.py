"""The Postgres sink: the shared contract against a real server, then what only SQL can break.

The tests that need a server are marked ``postgres`` and skip unless the variable
``CATALOG_SYNC_TEST_POSTGRES_DSN`` names a *throw-away* server (a Postgres service container in
CI, a scratch cluster locally). They create one schema of their own and drop it at the end; no
other schema or table is touched. The statement and option tests need neither a server nor the
driver.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catalog_sync import registry
from catalog_sync.errors import ConfigError, SinkError
from catalog_sync.models import OWNABLE_FIELDS, MaterialRecord
from catalog_sync.sinks import postgres as postgres_module
from catalog_sync.sinks.base import BackupReceipt, Batch, Sink, WriteReport
from catalog_sync.sinks.material import material_batch
from catalog_sync.sinks.postgres import PostgresSink, qualified_name, read_table, upsert_statement
from catalog_sync.testing.sink_contract import (
    CONTRACT_CHECKS,
    CONTRACT_KINDS,
    GENERIC_SHAPE,
    ContractShape,
    HarnessFactory,
    SinkHarness,
    contract_batch,
    contract_row,
)
from tests.sink_harnesses import SHAPES, Mutant, check_fails

DSN_VARIABLE = "CATALOG_SYNC_TEST_DSN"
ALL = frozenset(OWNABLE_FIELDS)


# --- pieces that need no server -------------------------------------------------------------------


def statement_for(
    *, keep: frozenset[str] = frozenset(), owned: frozenset[str] | None = None
) -> str:
    batch = contract_batch(
        [], owned=frozenset(CONTRACT_KINDS) if owned is None else owned, keep_existing_if_null=keep
    )
    return upsert_statement("materials", qualified_name("public", "materials"), batch)


def test_the_statement_is_an_upsert_that_cannot_delete_or_replace() -> None:
    statement = statement_for()
    assert 'ON CONFLICT ("source", "source_id") DO UPDATE SET' in statement
    for forbidden in ("DELETE", "REPLACE", "TRUNCATE", "DROP"):
        assert forbidden not in statement.upper()


def test_only_owned_columns_are_assigned_and_each_value_is_cast_by_its_kind() -> None:
    statement = statement_for(owned=frozenset({"name", "price", "seen_at"}))
    assignments = statement.split("DO UPDATE SET ")[1].split(" WHERE ")[0]
    assert assignments.count("excluded.") == 3
    assert '"weight"' not in statement and '"rank"' not in statement
    assert "VALUES (%s, %s, %s, %s::jsonb, %s::timestamptz)" in statement


def test_keep_existing_is_coalesce_for_scalars_and_a_case_for_json() -> None:
    statement = statement_for(keep=frozenset({"name", "tags"}))
    assert 'COALESCE(excluded."name", "materials"."name")' in statement
    assert "CASE WHEN" in statement and "'[]'::jsonb" in statement
    unkept = statement_for()
    assert "COALESCE" not in unkept and "CASE" not in unkept


def test_an_unchanged_row_is_a_no_op_and_an_insert_is_told_from_an_update() -> None:
    statement = statement_for()
    assert "IS DISTINCT FROM" in statement
    assert statement.endswith("RETURNING (xmax = 0)")


def test_a_batch_that_owns_nothing_inserts_the_key_and_updates_nothing() -> None:
    statement = statement_for(owned=frozenset())
    assert "DO NOTHING" in statement and "DO UPDATE" not in statement


def test_names_are_quoted_and_the_schema_is_part_of_the_target() -> None:
    assert qualified_name(None, "materials") == '"materials"'
    assert qualified_name("shop", "materials") == '"shop"."materials"'
    assert qualified_name(None, 'we"ird') == '"we""ird"'


def test_the_sink_is_registered_under_the_postgres_name() -> None:
    assert "postgres" in registry.available(registry.SINKS_GROUP)
    assert registry.sink_factory("postgres") == PostgresSink.from_options


def test_options_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="dsn_env"):
        PostgresSink.from_options({}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="no 'dsn' option") as inline:
        PostgresSink.from_options({"dsn": "postgresql://u:hunter2@h/db"}, base_dir=tmp_path)
    assert "hunter2" not in str(inline.value)
    with pytest.raises(ConfigError, match="unknown option"):
        PostgresSink.from_options({"dsn_env": "X", "ssl": True}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="'table'"):
        PostgresSink.from_options({"dsn_env": "X", "table": "a b"}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="'schema'"):
        PostgresSink.from_options({"dsn_env": "X", "schema": "a;b"}, base_dir=tmp_path)
    sink = PostgresSink.from_options(
        {"dsn_env": "X", "table": "items", "schema": "shop"}, base_dir=tmp_path
    )
    assert (sink.dsn_env, sink.table, sink.schema) == ("X", "items", "shop")


def test_a_table_name_too_long_for_its_backup_names_is_refused() -> None:
    longest = "t" * 33  # 33 + "__backup_" + a 21-character stamp = Postgres's 63-byte limit
    assert PostgresSink("X", table=longest).table == longest
    with pytest.raises(ConfigError, match="at most 33"):
        PostgresSink("X", table="t" * 34)


def test_an_unset_connection_variable_is_named_in_the_error() -> None:
    sink = PostgresSink("CS_NOT_SET_ANYWHERE", environ={})
    with pytest.raises(ConfigError, match="CS_NOT_SET_ANYWHERE is not set"):
        sink.open()


def test_use_before_open_is_refused() -> None:
    sink = PostgresSink("X", environ={})
    with pytest.raises(SinkError, match="not open"):
        sink.backup()


def test_a_connection_failure_does_not_echo_the_password(tmp_path: Path) -> None:
    pytest.importorskip("psycopg")
    secret = "canary-db-password"
    unreachable = f"postgresql://someone:{secret}@/db?host={tmp_path / 'no-such-socket-dir'}"
    with pytest.raises(SinkError) as refused:
        PostgresSink("X", environ={"X": unreachable}).open()
    assert secret not in str(refused.value) and secret not in repr(refused.value)
    with pytest.raises(SinkError, match="not valid") as malformed:
        PostgresSink(
            "X", environ={"X": f"postgresql://someone:{secret}@/db?no_such_option=1"}
        ).open()
    assert secret not in str(malformed.value)


class RecordingCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class RecordingConnection:
    """Just enough of a psycopg connection for `PostgresSink.upsert`; keeps every statement."""

    def __init__(self, rollback: type[BaseException]) -> None:
        self.calls: list[tuple[str, object]] = []
        self._rollback = rollback

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with suppress(self._rollback):  # what a real transaction block does with psycopg.Rollback
            yield

    @contextmanager
    def pipeline(self) -> Iterator[None]:
        yield

    def execute(self, sql: str, params: object = None) -> RecordingCursor:
        self.calls.append((sql, params))
        if "to_regclass" in sql and sql.startswith("SELECT to_regclass"):
            return RecordingCursor([(False,)])
        if "pg_attribute" in sql:
            return RecordingCursor([("source",), ("source_id",), ("name",)])
        return RecordingCursor([])

    def close(self) -> None:
        pass


def sink_on_recording_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PostgresSink, list[tuple[str, object]]]:
    driver = pytest.importorskip("psycopg")
    connection = RecordingConnection(driver.Rollback)
    monkeypatch.setattr(postgres_module, "connect", lambda _dsn: connection)
    sink = PostgresSink("X", table="materials", schema="public", environ={"X": "unused"})
    sink.open()
    sink.backup()
    connection.calls.clear()
    return sink, connection.calls


def test_a_real_load_takes_the_tables_advisory_lock_before_anything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without it two loads into one table interleave and the second pre-image copy (READ
    COMMITTED) misses the first load's rows. Needs no server: the statement order is the claim."""
    sink, calls = sink_on_recording_connection(monkeypatch)
    sink.upsert(named("A", "a"))
    statement, params = calls[0]
    assert statement.startswith("SELECT pg_advisory_xact_lock(")
    assert params == ('catalog-sync:"public"."materials"',)
    assert all("pg_advisory" not in other for other, _ in calls[1:])


def test_a_dry_run_does_not_queue_behind_a_real_load(monkeypatch: pytest.MonkeyPatch) -> None:
    sink, calls = sink_on_recording_connection(monkeypatch)
    sink.upsert(named("A", "a"), dry_run=True)
    assert calls, "the dry run did run its statements"
    assert not any("pg_advisory" in statement for statement, _ in calls)


# --- the harness ----------------------------------------------------------------------------------


TABLE_NUMBERS = itertools.count()  # module-wide: every table of the session's schema is new


@pytest.fixture
def make_sink(postgres_dsn: str, postgres_schema: str) -> Callable[..., PostgresSink]:
    ticks = itertools.count()
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def build(table: str | None = None) -> PostgresSink:
        return PostgresSink(
            DSN_VARIABLE,
            table=table or f"t{next(TABLE_NUMBERS)}",
            schema=postgres_schema,
            # One tick per call: two backups never share a name, yet the names are predictable.
            clock=lambda: start + timedelta(microseconds=next(ticks)),
            environ={DSN_VARIABLE: postgres_dsn},
        )

    return build


def harness(
    make_sink: Callable[..., PostgresSink],
    dsn: str,
    schema: str,
    shape: ContractShape = GENERIC_SHAPE,
) -> HarnessFactory:
    def make(_workdir: Path) -> SinkHarness:
        sink = make_sink()

        def read_backup(receipt: BackupReceipt) -> dict[tuple[str, str], dict[str, object]]:
            assert receipt.table is not None, "a sink with rows must produce a backup table"
            return read_table(dsn, receipt.table, schema)

        return SinkHarness(sink, lambda: read_table(dsn, sink.table, schema), read_backup, shape)

    return make


pg = pytest.mark.postgres


@pg
@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
@pytest.mark.parametrize("check", CONTRACT_CHECKS, ids=lambda check: check.__name__)
def test_postgres_honours_the_contract(
    check: Callable[[HarnessFactory, Path], None],
    shape: ContractShape,
    make_sink: Callable[..., PostgresSink],
    postgres_dsn: str,
    postgres_schema: str,
    tmp_path: Path,
) -> None:
    check(harness(make_sink, postgres_dsn, postgres_schema, shape), tmp_path)


# --- the contract suite must be able to fail against Postgres too (CODING-STYLE R.7) --------------


class DeletesAbsent(Mutant):
    """Breaks the 'never delete' rule the way a sloppy refresh would: DELETE what is absent."""

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> WriteReport:
        report = self.inner.upsert(batch, dry_run=dry_run)
        sink = self.inner
        assert isinstance(sink, PostgresSink)
        for source, source_id in report.absent:
            sink._connection().execute(
                f"DELETE FROM {qualified_name(sink.schema, sink.table)} "
                "WHERE source = %s AND source_id = %s",
                (source, source_id),
            )
        return report


class BackupIsTheLiveTable(Mutant):
    def backup(self) -> BackupReceipt:
        receipt = self.inner.backup()
        return BackupReceipt(path=None, rows_before=receipt.rows_before, table=self.inner.table)


class BackupCopiesNothing(PostgresSink):
    def _copy_pre_images(self, *_args: object, **_kwargs: object) -> None:
        return None


PgMutantBuilder = Callable[[PostgresSink], Sink]
PG_MUTANTS: list[tuple[str, PgMutantBuilder, str]] = [
    (
        "replaces rows wholesale",
        lambda sink: Mutant(sink, "postgres", rewrite=_replace_all),
        "check_unowned_fields_untouched",
    ),
    (
        "ignores keep_existing_if_null",
        lambda sink: Mutant(sink, "postgres", rewrite=_forget_keep),
        "check_keep_existing_if_null",
    ),
    (
        "deletes rows the source no longer returns",
        lambda sink: DeletesAbsent(sink, "postgres"),
        "check_never_deletes_rows",
    ),
    (
        "lets a load run without a backup",
        lambda sink: Mutant(sink, "postgres", auto_backup=True),
        "check_backup_precedes_upsert",
    ),
    (
        "hands out the live table as its backup",
        lambda sink: BackupIsTheLiveTable(sink, "postgres"),
        "check_backup_precedes_upsert",
    ),
    (
        "reports absent rows for incremental batches",
        lambda sink: Mutant(sink, "postgres", rewrite=_always_full),
        "check_never_deletes_rows",
    ),
    (
        "writes during a dry run",
        lambda sink: Mutant(sink, "postgres", ignore_dry_run=True),
        "check_dry_run_writes_nothing",
    ),
]


def _replace_all(batch: Batch) -> Batch:
    from dataclasses import replace

    return replace(batch, owned=frozenset(CONTRACT_KINDS), keep_existing_if_null=frozenset())


def _forget_keep(batch: Batch) -> Batch:
    from dataclasses import replace

    return replace(batch, keep_existing_if_null=frozenset())


def _always_full(batch: Batch) -> Batch:
    from dataclasses import replace

    return replace(batch, is_full_snapshot=True)


def failing_checks(
    make_sink: Callable[..., PostgresSink],
    dsn: str,
    schema: str,
    tmp_path: Path,
    wrap: PgMutantBuilder | None,
    sink_class: type[PostgresSink] = PostgresSink,
) -> set[str]:
    failed: set[str] = set()
    for number, check in enumerate(CONTRACT_CHECKS):

        def make(workdir: Path) -> SinkHarness:
            base = harness(make_sink, dsn, schema)(workdir)
            real = base.sink
            assert isinstance(real, PostgresSink)
            if sink_class is not PostgresSink:
                real = sink_class(
                    real.dsn_env, table=real.table, schema=real.schema, environ=real._environ
                )
            return SinkHarness(wrap(real) if wrap else real, base.read_rows, base.read_backup)

        if check_fails(check, make, tmp_path / str(number)):
            failed.add(check.__name__)
    return failed


@pg
def test_the_real_sink_fails_no_check(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str, tmp_path: Path
) -> None:
    assert failing_checks(make_sink, postgres_dsn, postgres_schema, tmp_path, None) == set()


@pg
@pytest.mark.parametrize(
    ("build", "caught_by"),
    [(build, caught_by) for _, build, caught_by in PG_MUTANTS],
    ids=[description for description, _, _ in PG_MUTANTS],
)
def test_each_contract_violation_turns_the_intended_check_red(
    build: PgMutantBuilder,
    caught_by: str,
    make_sink: Callable[..., PostgresSink],
    postgres_dsn: str,
    postgres_schema: str,
    tmp_path: Path,
) -> None:
    assert caught_by in failing_checks(make_sink, postgres_dsn, postgres_schema, tmp_path, build)


@pg
def test_a_backup_that_copies_nothing_turns_the_backup_check_red(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str, tmp_path: Path
) -> None:
    red = failing_checks(
        make_sink, postgres_dsn, postgres_schema, tmp_path, None, sink_class=BackupCopiesNothing
    )
    assert "check_backup_precedes_upsert" in red


# --- what only SQL can break ----------------------------------------------------------------------


def query(dsn: str, sql: str, *params: object) -> list[tuple[object, ...]]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(sql, params or None).fetchall()


def run(dsn: str, sql: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)


def open_sink(make_sink: Callable[..., PostgresSink], **kwargs: str) -> PostgresSink:
    sink = make_sink(**kwargs)
    sink.open()
    sink.backup()
    return sink


def named(source_id: str, name: str) -> Batch:
    return contract_batch([contract_row(source_id, name=name)], owned=frozenset({"name"}))


def target(sink: PostgresSink) -> str:
    return qualified_name(sink.schema, sink.table)


@pg
def test_each_kind_gets_its_native_column_type(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(contract_batch([contract_row("A", name="a")]))
    types = dict(
        query(
            postgres_dsn,
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            postgres_schema,
            sink.table,
        )
    )
    assert types == {
        "source": "text",
        "source_id": "text",
        "name": "text",
        "price": "jsonb",
        "rank": "bigint",
        "seen_at": "timestamp with time zone",
        "tags": "jsonb",
        "weight": "double precision",
    }
    keys = query(
        postgres_dsn,
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary "
        "ORDER BY a.attname",
        target(sink),
    )
    assert keys == [("source",), ("source_id",)]


@pg
def test_a_timestamp_keeps_its_instant_and_comes_back_in_utc(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(
        contract_batch(
            [contract_row("A", seen_at="2026-03-04T12:00:00.250000+08:00")],
            owned=frozenset({"seen_at"}),
        )
    )
    stored = read_table(postgres_dsn, sink.table, postgres_schema)[("contract", "A")]
    assert stored["seen_at"] == "2026-03-04T04:00:00.250000+00:00"


@pg
def test_a_field_the_table_has_no_column_for_yet_gets_one_and_old_rows_stay_null(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(named("A", "a"))
    sink.upsert(
        contract_batch([contract_row("B", name="b", rank=4)], owned=frozenset({"name", "rank"}))
    )
    rows = read_table(postgres_dsn, sink.table, postgres_schema)
    assert rows[("contract", "A")]["rank"] is None
    assert rows[("contract", "B")]["rank"] == 4


@pg
def test_columns_the_tool_does_not_know_survive_an_upsert(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    table = f"hand_made_{next(TABLE_NUMBERS)}"
    run(
        postgres_dsn,
        f'CREATE TABLE "{postgres_schema}"."{table}" (source text NOT NULL, '
        "source_id text NOT NULL, name text, stock_meters double precision, "
        "PRIMARY KEY (source, source_id))",
    )
    run(
        postgres_dsn,
        f"INSERT INTO \"{postgres_schema}\".\"{table}\" VALUES ('contract', 'A', 'old', 12.5)",
    )
    sink = open_sink(make_sink, table=table)
    assert sink.backup().rows_before == 1
    sink.upsert(named("A", "new"))
    assert query(postgres_dsn, f'SELECT name, stock_meters FROM "{postgres_schema}"."{table}"') == [
        ("new", 12.5)
    ]


@pg
def test_a_custom_schema_and_table_are_used(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink, table="catalog_items")
    sink.upsert(named("A", "a"))
    assert query(postgres_dsn, f'SELECT name FROM "{postgres_schema}"."catalog_items"') == [("a",)]


# --- the point of ON CONFLICT: parents are changed in place ---------------------------------------


def add_child_table(dsn: str, schema: str, parent: str) -> str:
    child = f"notes_{next(TABLE_NUMBERS)}"
    run(
        dsn,
        f'CREATE TABLE "{schema}"."{child}" (id serial PRIMARY KEY, source text NOT NULL, '
        "source_id text NOT NULL, body text, FOREIGN KEY (source, source_id) "
        f'REFERENCES "{schema}"."{parent}" (source, source_id) ON DELETE CASCADE)',
    )
    run(
        dsn,
        f'INSERT INTO "{schema}"."{child}" (source, source_id, body) '
        "VALUES ('contract', 'A', 'keep me')",
    )
    return child


@pg
def test_child_rows_with_on_delete_cascade_survive_a_re_upsert(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(named("A", "a"))
    child = add_child_table(postgres_dsn, postgres_schema, sink.table)
    sink.upsert(named("A", "changed"))
    assert query(postgres_dsn, f'SELECT body FROM "{postgres_schema}"."{child}"') == [("keep me",)]
    assert query(postgres_dsn, f'SELECT name FROM "{postgres_schema}"."{sink.table}"') == [
        ("changed",)
    ]


class ReplacingPostgresSink(PostgresSink):
    """The classic mistake: delete the row and insert it again."""

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> WriteReport:
        conn = self._connection()
        for row in batch.rows:
            conn.execute(
                f"DELETE FROM {target(self)} WHERE source = %s AND source_id = %s",
                (row["source"], row["source_id"]),
            )
        return super().upsert(batch, dry_run=dry_run)


@pg
def test_the_cascade_check_can_fail_delete_and_insert_does_wipe_the_child(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    plain = make_sink()
    sink = ReplacingPostgresSink(
        plain.dsn_env, table=plain.table, schema=plain.schema, environ=plain._environ
    )
    sink.open()
    sink.backup()
    PostgresSink.upsert(sink, named("A", "a"))
    child = add_child_table(postgres_dsn, postgres_schema, sink.table)
    sink.upsert(named("A", "changed"))
    assert query(postgres_dsn, f'SELECT count(*) FROM "{postgres_schema}"."{child}"') == [(0,)], (
        "with delete-then-insert the cascade removes the child; the test above would then be red"
    )


# --- backup ---------------------------------------------------------------------------------------


def tables_like(dsn: str, schema: str, prefix: str) -> list[str]:
    rows = query(
        dsn,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s "
        "AND table_name LIKE %s ORDER BY table_name",
        schema,
        prefix + "%",
    )
    return [str(row[0]) for row in rows]


@pg
def test_the_backup_holds_the_pre_load_version_of_the_rows_the_load_changes_and_no_others(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(
        contract_batch(
            [
                contract_row("A", name="a0"),
                contract_row("B", name="b0"),
                contract_row("C", name="c0"),
            ]
        )
    )
    sink.close()

    sink.open()
    receipt = sink.backup()
    assert receipt.rows_before == 3 and receipt.path is None
    assert receipt.table == f"{sink.table}__backup_20260101T000000000000"
    assert receipt.location == receipt.table
    sink.upsert(
        contract_batch(
            [contract_row("A", name="a1"), contract_row("D", name="d1")],
            owned=frozenset({"name"}),
        )
    )
    assert receipt.table is not None
    saved = read_table(postgres_dsn, receipt.table, postgres_schema)
    assert {key: row["name"] for key, row in saved.items()} == {("contract", "A"): "a0"}, (
        "A is the only existing row the load touched; D is new and has no earlier state"
    )


@pg
def test_a_second_upsert_of_the_same_load_does_not_overwrite_a_pre_image_or_invent_one(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(named("A", "a0"))
    sink.close()
    sink.open()
    receipt = sink.backup()
    sink.upsert(named("A", "a1"))
    sink.upsert(named("A", "a2"))
    sink.upsert(named("N", "created by this load"))
    sink.upsert(named("N", "created and then changed by this load"))
    assert receipt.table is not None
    saved = read_table(postgres_dsn, receipt.table, postgres_schema)
    assert {key: row["name"] for key, row in saved.items()} == {("contract", "A"): "a0"}


class SlowLoad(PostgresSink):
    """A load that stays inside its transaction, after copying its pre-images, until released."""

    def __init__(
        self,
        dsn_env: str,
        *,
        inside: threading.Event,
        release: threading.Event,
        table: str,
        schema: str,
        clock: Callable[[], datetime],
        environ: dict[str, str],
    ) -> None:
        super().__init__(dsn_env, table=table, schema=schema, clock=clock, environ=environ)
        self._inside = inside
        self._release = release

    def _copy_pre_images(self, conn, target, keys):  # type: ignore[no-untyped-def]
        super()._copy_pre_images(conn, target, keys)
        self._inside.set()
        assert self._release.wait(30)


@pg
def test_loads_into_one_table_run_one_after_the_other_so_the_second_backup_sees_the_first(
    postgres_dsn: str, postgres_schema: str
) -> None:
    """Two profiles loading one table. The first load is held inside its transaction; the second
    must queue behind it, and so copy the first load's committed rows as its pre-images."""
    table = f"t{next(TABLE_NUMBERS)}"
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def at(second: int) -> dict[str, object]:
        return {
            "table": table,
            "schema": postgres_schema,
            "clock": lambda: start + timedelta(seconds=second),
            "environ": {DSN_VARIABLE: postgres_dsn},
        }

    seed = PostgresSink(DSN_VARIABLE, **at(0))  # type: ignore[arg-type]
    seed.open()
    seed.backup()
    seed.upsert(named("A", "a0"))
    seed.close()
    inside, release = threading.Event(), threading.Event()
    first = SlowLoad(DSN_VARIABLE, inside=inside, release=release, **at(1))  # type: ignore[arg-type]
    second = PostgresSink(DSN_VARIABLE, **at(2))  # type: ignore[arg-type]
    first.open()
    first.backup()
    second.open()
    receipt = second.backup()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_load = pool.submit(first.upsert, named("A", "a1"))
        assert inside.wait(30), "the first load never got to its pre-image copy"
        second_load = pool.submit(second.upsert, named("A", "a2"))
        time.sleep(0.5)
        assert not second_load.done(), "the second load must wait for the first to commit"
        release.set()
        first_load.result(timeout=30)
        second_load.result(timeout=30)
    first.close()
    second.close()
    assert receipt.table is not None
    saved = read_table(postgres_dsn, receipt.table, postgres_schema)
    assert {key: row["name"] for key, row in saved.items()} == {("contract", "A"): "a1"}


@pg
def test_a_second_backup_in_one_session_covers_the_rows_made_since_the_first(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = make_sink()
    sink.open()
    sink.backup()  # no table yet
    sink.upsert(named("A", "made by the first load step"))
    receipt = sink.backup()  # now A exists, and the next change to it has a before
    sink.upsert(named("A", "changed"))
    assert receipt.table is not None
    saved = read_table(postgres_dsn, receipt.table, postgres_schema)
    assert {key: row["name"] for key, row in saved.items()} == {
        ("contract", "A"): "made by the first load step"
    }


def test_a_failed_connection_leaves_the_sink_closed(tmp_path: Path) -> None:
    pytest.importorskip("psycopg")
    sink = PostgresSink("X", environ={"X": f"postgresql:///db?host={tmp_path / 'nowhere'}"})
    with pytest.raises(SinkError, match="cannot connect"):
        sink.open()
    with pytest.raises(SinkError, match="not open"):
        sink.backup()


@pg
def test_the_backup_table_has_the_key_as_primary_key_and_no_foreign_keys(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(named("A", "a"))
    child = add_child_table(postgres_dsn, postgres_schema, sink.table)
    sink.close()
    sink.open()
    receipt = sink.backup()
    assert receipt.table is not None
    constraints = query(
        postgres_dsn,
        "SELECT contype FROM pg_constraint WHERE conrelid = %s::regclass ORDER BY contype",
        qualified_name(postgres_schema, receipt.table),
    )
    assert constraints == [("p",)], "a primary key, and nothing that could cascade"
    assert child not in receipt.table


@pg
def test_a_first_load_into_a_missing_table_has_nothing_to_back_up(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = make_sink()
    sink.open()
    assert sink.backup() == BackupReceipt(path=None, rows_before=0)
    sink.upsert(named("A", "a"))
    assert tables_like(postgres_dsn, postgres_schema, sink.table + "__backup_") == []


@pg
def test_a_backup_never_overwrites_an_earlier_one(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    frozen = datetime(2026, 1, 1, tzinfo=UTC)
    base = make_sink()
    sink = PostgresSink(
        base.dsn_env,
        table=base.table,
        schema=base.schema,
        clock=lambda: frozen,
        environ=base._environ,
    )
    sink.open()
    sink.backup()
    sink.upsert(named("A", "a"))
    first = sink.backup()
    with pytest.raises(SinkError, match="already exists"):
        sink.backup()  # same instant: refusing beats destroying the earlier backup
    assert first.table is not None
    assert tables_like(postgres_dsn, postgres_schema, sink.table + "__backup_") == [first.table]


@pg
def test_backups_are_kept_one_per_load(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(named("A", "a"))
    for number in range(3):
        sink.close()
        sink.open()
        sink.backup()
        sink.upsert(named("A", f"a{number}"))
    assert len(tables_like(postgres_dsn, postgres_schema, sink.table + "__backup_")) == 3


# --- dry run --------------------------------------------------------------------------------------


@pg
def test_a_dry_run_leaves_no_table_column_row_or_backup_behind(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = make_sink()
    sink.open()
    report = sink.upsert(named("A", "a"), dry_run=True)
    assert report.inserted == 1
    assert tables_like(postgres_dsn, postgres_schema, sink.table) == [], "not even the table"

    sink.backup()
    sink.upsert(named("A", "a"))
    sink.close()
    sink.open()
    dry = sink.upsert(
        contract_batch(
            [contract_row("A", name="x", rank=1), contract_row("B", name="b", rank=2)],
            owned=frozenset({"name", "rank"}),
        ),
        dry_run=True,
    )
    assert (dry.inserted, dry.updated) == (1, 1)
    columns = query(
        postgres_dsn,
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
        "AND table_name = %s ORDER BY column_name",
        postgres_schema,
        sink.table,
    )
    assert columns == [("name",), ("source",), ("source_id",)], "the new column was rolled back"
    assert tables_like(postgres_dsn, postgres_schema, sink.table + "__backup_") == []


# --- transactions and reporting -------------------------------------------------------------------


@pg
def test_a_failure_halfway_through_a_batch_rolls_back_the_rows_and_the_pre_images(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(named("A", "a0"))
    sink.close()
    sink.open()
    receipt = sink.backup()
    run(
        postgres_dsn,
        f'CREATE FUNCTION "{postgres_schema}".no_b() RETURNS trigger LANGUAGE plpgsql AS '
        "$$ BEGIN RAISE EXCEPTION 'no B'; END $$",
    )
    run(
        postgres_dsn,
        f'CREATE TRIGGER no_b BEFORE INSERT ON "{postgres_schema}"."{sink.table}" '
        f"FOR EACH ROW WHEN (NEW.source_id = 'B') EXECUTE FUNCTION \"{postgres_schema}\".no_b()",
    )
    both = contract_batch(
        [contract_row("A", name="a1"), contract_row("B", name="b1")], owned=frozenset({"name"})
    )
    with pytest.raises(SinkError, match="no B"):
        sink.upsert(both)
    rows = read_table(postgres_dsn, sink.table, postgres_schema)
    assert {key: row["name"] for key, row in rows.items()} == {("contract", "A"): "a0"}
    assert receipt.table is not None
    assert read_table(postgres_dsn, receipt.table, postgres_schema) == {}, "pre-images rolled back"
    sink.upsert(named("C", "c"))  # the connection is usable again
    assert ("contract", "C") in read_table(postgres_dsn, sink.table, postgres_schema)


@pg
def test_text_postgres_cannot_store_is_refused_as_a_sink_error_and_rolled_back(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    batch = contract_batch(
        [contract_row("A", name="fine"), contract_row("B", name="has a \x00 in it")],
        owned=frozenset({"name"}),
    )
    with pytest.raises(SinkError, match="undid it"):
        sink.upsert(batch)
    assert read_table(postgres_dsn, sink.table, postgres_schema) == {}


@pg
def test_updated_and_unchanged_are_told_apart_by_the_database(
    make_sink: Callable[..., PostgresSink],
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(
        contract_batch([contract_row("A", name="a", tags=("t",)), contract_row("B", name="b")])
    )
    report = sink.upsert(
        contract_batch([contract_row("A", name="a", tags=("t",)), contract_row("B", name="B!")])
    )
    assert (report.inserted, report.updated, report.unchanged) == (0, 1, 1)


@pg
def test_a_batch_larger_than_one_pipeline_round_is_counted_exactly(
    make_sink: Callable[..., PostgresSink],
) -> None:
    sink = open_sink(make_sink)
    rows = [contract_row(f"id-{number:05d}", name=f"n{number}") for number in range(1234)]
    first = sink.upsert(contract_batch(rows, owned=frozenset({"name"})))
    assert (first.inserted, first.updated, first.unchanged) == (1234, 0, 0)
    again = sink.upsert(contract_batch(rows, owned=frozenset({"name"})))
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 1234)
    changed = [*rows[:600], *(contract_row(f"id-{n:05d}", name="new") for n in range(600, 1234))]
    third = sink.upsert(contract_batch(changed, owned=frozenset({"name"})))
    assert (third.inserted, third.updated, third.unchanged) == (0, 634, 600)


@pg
def test_absent_rows_are_listed_per_source_in_key_order(
    make_sink: Callable[..., PostgresSink],
) -> None:
    sink = open_sink(make_sink)
    sink.upsert(
        contract_batch(
            [
                contract_row("B", source="s1"),
                contract_row("A", source="s1"),
                contract_row("Z", source="s2"),
                contract_row("C", source="s1"),
            ]
        )
    )
    keep = contract_row("A", source="s1")
    report = sink.upsert(contract_batch([keep], is_full_snapshot=True))
    assert report.absent == (("s1", "B"), ("s1", "C")), "s2 was not part of this batch"


@pg
def test_material_records_round_trip_through_the_table(
    make_sink: Callable[..., PostgresSink], postgres_dsn: str, postgres_schema: str
) -> None:
    sink = open_sink(make_sink)
    stamp = datetime(2026, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    record = MaterialRecord(
        "demo", "1", name="n", weight_gsm=180.0, tags=("a",), fetched_at=stamp, normalized_at=stamp
    )
    sink.upsert(material_batch([record], owned=ALL))
    assert read_table(postgres_dsn, sink.table, postgres_schema)[("demo", "1")] == record.to_dict()


@pg
def test_the_connection_is_closed_with_the_sink(make_sink: Callable[..., PostgresSink]) -> None:
    sink = open_sink(make_sink)
    sink.close()
    sink.close()  # idempotent
    with pytest.raises(SinkError, match="not open"):
        sink.upsert(named("A", "a"), dry_run=True)
