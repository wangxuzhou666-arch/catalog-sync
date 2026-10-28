"""Behaviour specific to the CSV format; the shared contract is in test_sink_contract.py."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError, RecordFormatError, SinkError
from catalog_sync.sinks.base import BackupReceipt, FieldKind
from catalog_sync.sinks.csv import CsvSink, decode_cell, encode_cell
from catalog_sync.testing.sink_contract import CONTRACT_KINDS, contract_batch, contract_row
from tests.sink_harnesses import files_in


def open_sink(tmp_path: Path) -> CsvSink:
    sink = CsvSink(tmp_path / "catalog.csv")
    sink.open()
    sink.backup()
    return sink


def cells(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_a_new_file_has_the_key_columns_first_then_the_owned_fields_sorted(
    tmp_path: Path,
) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(contract_batch([contract_row("A", name="a", weight=1.5, tags=("x",))]))
    header, row = cells(tmp_path / "catalog.csv")
    assert header == ["source", "source_id", "name", "price", "rank", "seen_at", "tags", "weight"]
    assert row == ["contract", "A", "a", "", "", "", '["x"]', "1.5"]


def test_null_is_an_empty_cell_and_json_is_compact_with_sorted_keys(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    price = {"currency": "USD", "amount": "9.50"}
    sink.upsert(contract_batch([contract_row("A", price=price)]))
    _, row = cells(tmp_path / "catalog.csv")
    assert row[3] == '{"amount":"9.50","currency":"USD"}'
    assert row[2] == "", "a null name is an empty cell"


def test_columns_the_tool_does_not_know_survive_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "catalog.csv"
    path.write_text('source,source_id,name,stock_meters\ncontract,A,old,"12,5"\n', encoding="utf-8")
    sink = CsvSink(path)
    sink.open()
    sink.backup()
    sink.upsert(contract_batch([contract_row("A", name="new")], owned=frozenset({"name"})))
    header, row = cells(path)
    assert header == ["source", "source_id", "name", "stock_meters"]
    assert row == ["contract", "A", "new", "12,5"], "the hand-added column is not ours to alter"


def test_a_new_owned_column_is_appended_and_old_rows_get_an_empty_cell(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(contract_batch([contract_row("A", name="a")], owned=frozenset({"name"})))
    sink.upsert(
        contract_batch([contract_row("B", name="b", rank=4)], owned=frozenset({"name", "rank"}))
    )
    header, first, second = cells(tmp_path / "catalog.csv")
    assert header == ["source", "source_id", "name", "rank"]
    assert first == ["contract", "A", "a", ""]
    assert second == ["contract", "B", "b", "4"]


def test_values_with_commas_quotes_and_newlines_round_trip(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    awkward = 'He said "no", then\nleft'
    sink.upsert(contract_batch([contract_row("A", name=awkward)], owned=frozenset({"name"})))
    assert sink.read_rows(CONTRACT_KINDS)[("contract", "A")]["name"] == awkward


def test_non_ascii_text_is_written_as_utf8(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(contract_batch([contract_row("A", name="café")], owned=frozenset({"name"})))
    assert "café" in (tmp_path / "catalog.csv").read_text(encoding="utf-8")


def test_an_unchanged_batch_does_not_rewrite_the_file(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    batch = contract_batch([contract_row("A", name="a")])
    sink.upsert(batch)
    path = tmp_path / "catalog.csv"
    before = path.stat()
    sink.upsert(batch)
    after = path.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_a_row_that_gains_a_column_it_never_had_is_not_counted_as_updated(
    tmp_path: Path,
) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(contract_batch([contract_row("A", name="a")], owned=frozenset({"name"})))
    report = sink.upsert(
        contract_batch([contract_row("A", name="a")], owned=frozenset({"name", "rank"}))
    )
    assert (report.updated, report.unchanged) == (0, 1), "a null in a new column changes no value"


def test_an_empty_batch_creates_no_file(tmp_path: Path) -> None:
    open_sink(tmp_path).upsert(contract_batch([]))
    assert not (tmp_path / "catalog.csv").exists()


def test_a_dry_run_creates_neither_file_nor_directory(tmp_path: Path) -> None:
    sink = CsvSink(tmp_path / "deep" / "catalog.csv")
    sink.open()
    sink.upsert(contract_batch([contract_row("A", name="a")]), dry_run=True)
    assert files_in(tmp_path) == []


def test_backup_of_a_missing_file_is_an_empty_receipt(tmp_path: Path) -> None:
    sink = CsvSink(tmp_path / "catalog.csv")
    sink.open()
    assert sink.backup() == BackupReceipt(path=None, rows_before=0)


def test_output_and_backup_are_private(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(contract_batch([contract_row("A", name="a")]))
    receipt = sink.backup()
    assert receipt.path is not None
    assert (tmp_path / "catalog.csv").stat().st_mode & 0o077 == 0
    assert receipt.path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "no header row"),
        ("source,name\nx,y\n", "lacks the key column"),
        ("source,source_id,name,name\n", "duplicate column"),
        ("source,source_id\ns,1,extra\n", "more cells than columns"),
        ("source,source_id\n,1\n", "lacks 'source' and 'source_id'"),
        ("source,source_id\ns,1\ns,1\n", "duplicate key"),
    ],
)
def test_malformed_files_are_refused_before_anything_changes(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "catalog.csv"
    path.write_text(content, encoding="utf-8")
    sink = CsvSink(path)
    sink.open()
    with pytest.raises(RecordFormatError, match=message):
        sink.backup()
    assert path.read_text(encoding="utf-8") == content
    assert files_in(tmp_path) == [path]


def test_short_rows_are_read_as_empty_trailing_cells(tmp_path: Path) -> None:
    path = tmp_path / "catalog.csv"
    path.write_text("source,source_id,name\ncontract,A\n", encoding="utf-8")
    sink = CsvSink(path)
    sink.open()
    sink.backup()
    report = sink.upsert(contract_batch([contract_row("A", name=None)], owned=frozenset({"name"})))
    assert report.unchanged == 1


def test_use_before_open_is_refused(tmp_path: Path) -> None:
    sink = CsvSink(tmp_path / "catalog.csv")
    with pytest.raises(SinkError, match="not open"):
        sink.backup()
    sink.open()
    with pytest.raises(SinkError, match="backup"):
        sink.upsert(contract_batch([contract_row("A")]))


def test_options_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="'path'"):
        CsvSink.from_options({}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="unknown option"):
        CsvSink.from_options({"path": "x", "delimiter": ";"}, base_dir=tmp_path)
    assert CsvSink.from_options({"path": "c.csv"}, base_dir=tmp_path).path == tmp_path / "c.csv"


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (FieldKind.TEXT, "text"),
        (FieldKind.INTEGER, 7),
        (FieldKind.REAL, 0.1),
        (FieldKind.REAL, 150.0),
        (FieldKind.JSON, [1, {"a": None}]),
        (FieldKind.JSON, {}),
    ],
)
def test_cells_decode_to_what_was_encoded(kind: FieldKind, value: object) -> None:
    assert decode_cell(encode_cell(value, kind), kind) == value


# --- spreadsheet formula injection ----------------------------------------------------------------

FORMULA_STARTS = ["=", "+", "-", "@", "\t", "\r"]


@pytest.mark.parametrize("start", FORMULA_STARTS, ids=["eq", "plus", "minus", "at", "tab", "cr"])
def test_text_that_starts_like_a_formula_is_written_with_an_apostrophe(
    tmp_path: Path, start: str
) -> None:
    sink = open_sink(tmp_path)
    payload = f'{start}HYPERLINK("http://x.invalid","go")'
    sink.upsert(contract_batch([contract_row("A", name=payload)], owned=frozenset({"name"})))
    _, row = cells(tmp_path / "catalog.csv")
    assert row[2] == "'" + payload, "a spreadsheet must show this cell, not run it"
    assert sink.read_rows(CONTRACT_KINDS)[("contract", "A")]["name"] == payload


@pytest.mark.parametrize(
    "text", ["plain", "a=b", " =1", "x+y", "", "'quoted", "it's", "5-3", "email@example.com"]
)
def test_text_that_cannot_start_a_formula_is_written_as_it_is(text: str) -> None:
    assert encode_cell(text, FieldKind.TEXT) == text
    assert decode_cell(encode_cell(text, FieldKind.TEXT), FieldKind.TEXT) == (text or None)


@pytest.mark.parametrize("text", ["=1", "'=1", "''=1", "'+x", "'-", "-'", "@'", "'\t"])
def test_guarding_is_reversible_even_for_text_that_already_has_an_apostrophe(text: str) -> None:
    cell = encode_cell(text, FieldKind.TEXT)
    assert decode_cell(cell, FieldKind.TEXT) == text


def test_negative_numbers_are_numbers_not_formulas(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(
        contract_batch(
            [contract_row("A", weight=-1.5, rank=-3, name="n")], owned=frozenset({"weight", "rank"})
        )
    )
    _, row = cells(tmp_path / "catalog.csv")
    assert row[2:] == ["-3", "-1.5"], "an apostrophe would turn the number into text"
    assert encode_cell([-1], FieldKind.JSON) == "[-1]"


def test_the_key_columns_are_guarded_and_found_again(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    hostile_id = "=cmd|' /C calc'!A0"
    batch = contract_batch([contract_row(hostile_id, name="a")], owned=frozenset({"name"}))
    assert sink.upsert(batch).inserted == 1
    _, row = cells(tmp_path / "catalog.csv")
    assert row[1] == "'" + hostile_id
    assert sink.upsert(batch).unchanged == 1, "the guarded key must still identify its row"
    assert ("contract", hostile_id) in sink.read_rows(CONTRACT_KINDS)


def test_an_unchanged_guarded_value_is_not_rewritten(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    batch = contract_batch([contract_row("A", name="=1")], owned=frozenset({"name"}))
    sink.upsert(batch)
    assert sink.upsert(batch).unchanged == 1


def test_a_cell_an_older_version_wrote_unguarded_is_repaired_when_its_value_is_rewritten(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.csv"
    path.write_text("source,source_id,name\ncontract,=old,=1\n", encoding="utf-8")
    sink = CsvSink(path)
    sink.open()
    sink.backup()
    batch = contract_batch([contract_row("=old", name="=1")], owned=frozenset({"name"}))
    assert sink.upsert(batch).updated == 1, "the stored cell differs from what is written now"
    assert cells(path)[1] == ["contract", "'=old", "'=1"]


def test_the_timestamp_kind_is_text_for_the_guard() -> None:
    assert encode_cell("2026-01-01T00:00:00+00:00", FieldKind.TIMESTAMP) == (
        "2026-01-01T00:00:00+00:00"
    )
    assert encode_cell("-1", FieldKind.TIMESTAMP) == "'-1"


def test_a_second_load_into_the_same_csv_is_refused_while_the_first_is_open(
    tmp_path: Path,
) -> None:
    first = open_sink(tmp_path)
    second = CsvSink(tmp_path / "catalog.csv")
    second.open()
    with pytest.raises(SinkError, match="another catalog-sync process"):
        second.backup()
    first.close()
    second.backup()
