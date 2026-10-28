"""Behaviour specific to the SQLite sink; the shared contract is in test_sink_contract.py.

The foreign-key tests live here because only a SQL sink can wipe child rows by replacing a parent.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError, SinkError
from catalog_sync.models import OWNABLE_FIELDS, MaterialRecord
from catalog_sync.sinks.base import BackupReceipt, Batch
from catalog_sync.sinks.material import material_batch
from catalog_sync.sinks.sqlite import DEFAULT_TABLE, SqliteSink, read_table, upsert_statement
from catalog_sync.testing.sink_contract import contract_batch, contract_row

ALL = frozenset(OWNABLE_FIELDS)


def open_sink(tmp_path: Path, **kwargs: str) -> SqliteSink:
    sink = SqliteSink(tmp_path / "catalog.db", **kwargs)
    sink.open()
    sink.backup()
    return sink


def named(source_id: str, name: str) -> Batch:
    return contract_batch([contract_row(source_id, name=name)], owned=frozenset({"name"}))


def query(path: Path, sql: str, *params: object) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def declared_types(path: Path, table: str = DEFAULT_TABLE) -> dict[str, str]:
    return {row[1]: row[2] for row in query(path, f'PRAGMA table_info("{table}")')}


# --- schema -------------------------------------------------------------------------------------


def test_the_table_is_created_with_a_native_type_per_kind(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(contract_batch([contract_row("A", name="a", weight=1.5, rank=2, tags=("x",))]))
    assert declared_types(tmp_path / "catalog.db") == {
        "source": "TEXT",
        "source_id": "TEXT",
        "name": "TEXT",
        "price": "JSON_TEXT",
        "rank": "INTEGER",
        "seen_at": "TEXT",
        "tags": "JSON_TEXT",
        "weight": "REAL",
    }
    pk = [
        r[1]
        for r in query(tmp_path / "catalog.db", f'PRAGMA table_info("{DEFAULT_TABLE}")')
        if r[5]
    ]
    assert pk == ["source", "source_id"]


def test_json_is_stored_as_compact_text_and_read_back_decoded(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    price = {"currency": "USD", "amount": "9.50"}
    sink.upsert(contract_batch([contract_row("A", price=price, tags=("x", "y"))]))
    raw = query(tmp_path / "catalog.db", 'SELECT price, tags FROM "materials"')
    assert raw == [('{"amount":"9.50","currency":"USD"}', '["x","y"]')]
    assert read_table(tmp_path / "catalog.db")[("contract", "A")]["price"] == price


def test_a_field_the_table_has_no_column_for_yet_gets_one_and_old_rows_stay_null(
    tmp_path: Path,
) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(named("A", "a"))
    sink.upsert(
        contract_batch([contract_row("B", name="b", rank=4)], owned=frozenset({"name", "rank"}))
    )
    rows = read_table(tmp_path / "catalog.db")
    assert rows[("contract", "A")]["rank"] is None
    assert rows[("contract", "B")]["rank"] == 4


def test_columns_the_tool_does_not_know_survive_an_upsert(tmp_path: Path) -> None:
    path = tmp_path / "catalog.db"
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE "materials" (source TEXT NOT NULL, source_id TEXT NOT NULL, name TEXT, '
        "stock_meters REAL, PRIMARY KEY (source, source_id))"
    )
    conn.execute("INSERT INTO materials VALUES ('contract', 'A', 'old', 12.5)")
    conn.commit()
    conn.close()
    sink = SqliteSink(path)
    sink.open()
    sink.backup()
    sink.upsert(named("A", "new"))
    assert query(path, "SELECT name, stock_meters FROM materials") == [("new", 12.5)]


def test_a_custom_table_name_is_used(tmp_path: Path) -> None:
    sink = open_sink(tmp_path, table="catalog")
    sink.upsert(named("A", "a"))
    assert query(tmp_path / "catalog.db", "SELECT name FROM catalog") == [("a",)]


@pytest.mark.parametrize("bad", ["a b", "x;drop", '"q"', "1abc", ""])
def test_table_names_that_are_not_plain_identifiers_are_refused(bad: str) -> None:
    with pytest.raises(SinkError, match="not a plain SQL identifier"):
        SqliteSink(Path("x.db"), table=bad)


def test_field_names_that_are_not_plain_identifiers_are_refused_before_any_sql(
    tmp_path: Path,
) -> None:
    sink = open_sink(tmp_path)
    from catalog_sync.sinks.base import FieldKind

    hostile = 'name"; DROP TABLE materials; --'
    row = {"source": "s", "source_id": "1", hostile: "x"}
    batch = Batch([row], owned=frozenset({hostile}), kinds={hostile: FieldKind.TEXT})
    with pytest.raises(SinkError, match="not a plain SQL identifier"):
        sink.upsert(batch)
    assert query(tmp_path / "catalog.db", "SELECT count(*) FROM sqlite_master") == [(0,)]


# --- the point of ON CONFLICT: parents are changed in place -------------------------------------


def test_a_row_is_updated_in_place_not_deleted_and_reinserted(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(named("A", "a"))
    sink.upsert(named("B", "b"))
    before = query(tmp_path / "catalog.db", "SELECT rowid, source_id FROM materials ORDER BY rowid")
    sink.upsert(named("A", "changed"))
    after = query(tmp_path / "catalog.db", "SELECT rowid, source_id FROM materials ORDER BY rowid")
    assert after == before, "a REPLACE would have moved A behind B"


def add_child_table(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
        "source_id TEXT NOT NULL, body TEXT, FOREIGN KEY (source, source_id) "
        "REFERENCES materials (source, source_id) ON DELETE CASCADE)"
    )
    conn.execute("INSERT INTO notes (source, source_id, body) VALUES ('contract', 'A', 'keep me')")
    conn.commit()
    conn.close()


def test_child_rows_with_on_delete_cascade_survive_a_re_upsert(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(named("A", "a"))
    add_child_table(tmp_path / "catalog.db")
    sink.upsert(named("A", "changed"))
    assert query(tmp_path / "catalog.db", "SELECT body FROM notes") == [("keep me",)]
    assert query(tmp_path / "catalog.db", "SELECT name FROM materials") == [("changed",)]


class ReplacingSqliteSink(SqliteSink):
    """The classic mistake: INSERT OR REPLACE, i.e. delete-then-insert."""

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> object:  # type: ignore[override]
        conn = self._connection()
        names = sorted(batch.owned)
        columns = ", ".join(f'"{n}"' for n in ["source", "source_id", *names])
        marks = ", ".join("?" for _ in range(len(names) + 2))
        for row in batch.rows:
            conn.execute(
                f'INSERT OR REPLACE INTO "{self.table}" ({columns}) VALUES ({marks})',  # noqa: S608 — test double with fixed field names
                [row["source"], row["source_id"], *(row[n] for n in names)],
            )
        return None


def test_the_cascade_check_can_fail_replace_semantics_do_wipe_the_child(tmp_path: Path) -> None:
    sink = ReplacingSqliteSink(tmp_path / "catalog.db")
    sink.open()
    sink.backup()
    SqliteSink.upsert(sink, named("A", "a"))
    add_child_table(tmp_path / "catalog.db")
    sink.upsert(named("A", "changed"))
    assert query(tmp_path / "catalog.db", "SELECT count(*) FROM notes") == [(0,)], (
        "with REPLACE the cascade deletes the child; the test above would then be red"
    )


def test_the_statement_never_uses_replace(tmp_path: Path) -> None:
    batch = material_batch([MaterialRecord("s", "1")], owned=ALL)
    statement = upsert_statement(DEFAULT_TABLE, batch)
    assert "ON CONFLICT" in statement
    assert "REPLACE" not in statement.upper()


# --- backup -------------------------------------------------------------------------------------


def test_backup_is_a_consistent_copy_of_the_whole_database_taken_before_the_load(
    tmp_path: Path,
) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(named("A", "before"))
    add_child_table(tmp_path / "catalog.db")
    receipt = sink.backup()
    sink.upsert(named("A", "after"))
    assert receipt.rows_before == 1
    assert receipt.path is not None
    assert query(receipt.path, "SELECT name FROM materials") == [("before",)]
    assert query(receipt.path, "SELECT body FROM notes") == [("keep me",)], "child tables too"


def test_backup_files_are_private(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(named("A", "a"))
    receipt = sink.backup()
    assert receipt.path is not None
    assert (tmp_path / "catalog.db").stat().st_mode & 0o077 == 0
    assert receipt.path.stat().st_mode & 0o077 == 0


def test_a_database_with_other_tables_but_no_ours_is_still_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "catalog.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE other (x)")
    conn.execute("INSERT INTO other VALUES (1)")
    conn.commit()
    conn.close()
    sink = SqliteSink(path)
    sink.open()
    receipt = sink.backup()
    assert receipt.rows_before == 0
    assert receipt.path is not None
    assert query(receipt.path, "SELECT x FROM other") == [(1,)]


def test_a_fresh_database_has_nothing_to_back_up_and_dry_run_leaves_no_file(
    tmp_path: Path,
) -> None:
    sink = SqliteSink(tmp_path / "catalog.db")
    sink.open()
    sink.upsert(named("A", "a"), dry_run=True)
    assert not (tmp_path / "catalog.db").exists()
    assert sink.backup() == BackupReceipt(path=None, rows_before=0)
    sink.upsert(named("A", "a"))
    assert (tmp_path / "catalog.db").exists()


def test_a_backup_never_overwrites_an_earlier_one(tmp_path: Path) -> None:
    frozen = datetime(2026, 1, 1, tzinfo=UTC)
    sink = SqliteSink(tmp_path / "catalog.db", clock=lambda: frozen)
    sink.open()
    sink.backup()
    sink.upsert(named("A", "a"))
    first = sink.backup()
    sink.upsert(named("B", "b"))
    with pytest.raises(SinkError, match="already exists"):
        sink.backup()  # same instant: refusing beats destroying the earlier backup
    assert first.path is not None
    assert query(first.path, "SELECT source_id FROM materials") == [("A",)]


# --- two loads into one file --------------------------------------------------------------------


def test_a_second_load_waits_for_the_first_so_its_backup_includes_what_the_first_wrote(
    tmp_path: Path,
) -> None:
    """Two profiles loading one database. Before, the second backup was taken while the first load
    was still to come, so restoring it would have silently undone the first load as well."""
    path = tmp_path / "catalog.db"
    first = SqliteSink(path)
    first.open()
    first.backup()
    second_is_waiting = threading.Event()

    def second_load() -> BackupReceipt:
        second = SqliteSink(path, lock_wait_seconds=30)
        second.open()
        second_is_waiting.set()
        receipt = second.backup()
        second.upsert(named("B", "b"))
        second.close()
        return receipt

    with ThreadPoolExecutor(max_workers=1) as pool:
        second = pool.submit(second_load)
        assert second_is_waiting.wait(10)
        time.sleep(0.3)  # long enough for the second load to be parked on the lock
        assert not second.done(), "the second load must wait for the first to commit"
        first.upsert(named("A", "a"))
        first.close()
        receipt = second.result(timeout=30)
    assert receipt.rows_before == 1 and receipt.path is not None
    assert query(receipt.path, "SELECT source_id FROM materials") == [("A",)]
    assert query(path, "SELECT source_id FROM materials ORDER BY source_id") == [("A",), ("B",)]


def test_a_load_that_cannot_get_the_lock_fails_cleanly_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "catalog.db"
    first = SqliteSink(path)
    first.open()
    first.backup()
    second = SqliteSink(path, lock_wait_seconds=0.1)
    second.open()
    with pytest.raises(SinkError, match=r"catalog\.db is locked by another writer.*Nothing"):
        second.backup()
    first.upsert(named("A", "a"))  # the first load never noticed
    assert query(path, "SELECT source_id FROM materials") == [("A",)]
    assert [p.name for p in tmp_path.iterdir() if ".bak-" in p.name] == [], "no half backup"


def test_a_backup_that_fails_gives_the_lock_back(tmp_path: Path) -> None:
    frozen = datetime(2026, 1, 1, tzinfo=UTC)
    sink = SqliteSink(tmp_path / "catalog.db", clock=lambda: frozen)
    sink.open()
    sink.backup()
    sink.upsert(named("A", "a"))
    sink.backup()
    sink.upsert(named("B", "b"))
    with pytest.raises(SinkError, match="already exists"):
        sink.backup()
    other = SqliteSink(tmp_path / "catalog.db", lock_wait_seconds=0.1)
    other.open()
    other.backup()  # would time out if the failed backup had kept the write lock


# --- dry run ------------------------------------------------------------------------------------


def test_a_dry_run_against_an_existing_database_leaves_its_bytes_alone(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(named("A", "a"))
    path = tmp_path / "catalog.db"
    before = path.read_bytes()
    siblings = sorted(p.name for p in tmp_path.iterdir())
    report = sink.upsert(
        contract_batch(
            [contract_row("A", name="x", rank=1), contract_row("B", name="b", rank=2)],
            owned=frozenset({"name", "rank"}),
        ),
        dry_run=True,
    )
    assert (report.inserted, report.updated) == (1, 1)
    assert path.read_bytes() == before, "not even a new column may reach the real file"
    assert sorted(p.name for p in tmp_path.iterdir()) == siblings, "no journal, no backup"


# --- transactions and reporting -----------------------------------------------------------------


def test_a_failure_halfway_through_a_batch_rolls_everything_back(tmp_path: Path) -> None:
    path = tmp_path / "catalog.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE materials (source TEXT NOT NULL, source_id TEXT NOT NULL, name TEXT, "
        "tag TEXT NOT NULL DEFAULT 'x' CHECK (tag != 'bad'), PRIMARY KEY (source, source_id))"
    )
    conn.execute(
        "CREATE TRIGGER no_b BEFORE INSERT ON materials WHEN NEW.source_id = 'B' "
        "BEGIN SELECT RAISE(ABORT, 'no B'); END"
    )
    conn.commit()
    conn.close()
    sink = SqliteSink(path)
    sink.open()
    sink.backup()
    good = contract_row("A", name="a")
    poisoned = contract_row("B", name="b")
    with pytest.raises(sqlite3.DatabaseError, match="no B"):
        sink.upsert(contract_batch([good, poisoned], owned=frozenset({"name"})))
    assert query(path, "SELECT count(*) FROM materials") == [(0,)], "A must not survive B's failure"
    sink.upsert(named("C", "c"))  # the connection is usable again after the rollback
    assert query(path, "SELECT source_id FROM materials") == [("C",)]


def test_updated_and_unchanged_are_told_apart_by_the_database(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(
        contract_batch([contract_row("A", name="a", tags=("t",)), contract_row("B", name="b")])
    )
    report = sink.upsert(
        contract_batch([contract_row("A", name="a", tags=("t",)), contract_row("B", name="B!")])
    )
    assert (report.inserted, report.updated, report.unchanged) == (0, 1, 1)


def test_absent_rows_are_listed_per_source_in_insertion_order(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(
        contract_batch(
            [
                contract_row("B", source="s1"),
                contract_row("A", source="s1"),
                contract_row("Z", source="s2"),
            ]
        )
    )
    report = sink.upsert(contract_batch([contract_row("A", source="s1")], is_full_snapshot=True))
    assert report.absent == (("s1", "B"),), "s2 was not part of this batch, so it is not 'absent'"


def test_a_batch_that_owns_nothing_still_inserts_the_key(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    batch = contract_batch([contract_row("A")], owned=frozenset())
    assert sink.upsert(batch).inserted == 1
    assert sink.upsert(batch).unchanged == 1


def test_material_records_round_trip_through_the_table(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    record = MaterialRecord("demo", "1", name="n", weight_gsm=180.0, tags=("a",))
    sink.upsert(material_batch([record], owned=ALL))
    assert read_table(tmp_path / "catalog.db")[("demo", "1")] == record.to_dict()


# --- lifecycle and options ----------------------------------------------------------------------


def test_use_before_open_is_refused_and_close_is_idempotent(tmp_path: Path) -> None:
    sink = SqliteSink(tmp_path / "catalog.db")
    with pytest.raises(SinkError, match="not open"):
        sink.backup()
    sink.open()
    with pytest.raises(SinkError, match="backup"):
        sink.upsert(named("A", "a"))
    sink.close()
    sink.close()


def test_options_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="'path'"):
        SqliteSink.from_options({}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="unknown option"):
        SqliteSink.from_options({"path": "x", "wal": True}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="'table'"):
        SqliteSink.from_options({"path": "x", "table": "a b"}, base_dir=tmp_path)
    sink = SqliteSink.from_options({"path": "out/c.db", "table": "items"}, base_dir=tmp_path)
    assert (sink.path, sink.table) == (tmp_path / "out" / "c.db", "items")
