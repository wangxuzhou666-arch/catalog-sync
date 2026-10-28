"""Behaviour specific to the JSONL format; the shared contract is in test_sink_contract.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError, RecordFormatError, SinkError
from catalog_sync.models import OWNABLE_FIELDS, MaterialRecord
from catalog_sync.sinks.base import BackupReceipt, Batch
from catalog_sync.sinks.jsonl import JsonlSink
from catalog_sync.sinks.lifecycle import LockedFileSink
from catalog_sync.sinks.material import material_batch
from catalog_sync.testing.sink_contract import CONTRACT_KINDS, contract_batch, contract_row
from tests.sink_harnesses import files_in

ALL = frozenset(OWNABLE_FIELDS)
SRC = Path(__file__).resolve().parent.parent / "src"


def read_jsonl(path: Path) -> dict[tuple[str, str], dict[str, object]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return {(row["source"], row["source_id"]): row for row in rows}


def open_sink(tmp_path: Path) -> JsonlSink:
    sink = JsonlSink(tmp_path / "catalog.jsonl")
    sink.open()
    sink.backup()
    return sink


def named(source_id: str, name: str) -> Batch:
    return contract_batch([contract_row(source_id, name=name)], owned=frozenset({"name"}))


def test_columns_the_tool_does_not_know_survive_an_upsert(tmp_path: Path) -> None:
    path = tmp_path / "catalog.jsonl"
    existing = {**MaterialRecord("demo", "A", name="old").to_dict(), "stock_meters": 12.5}
    path.write_text(json.dumps(existing) + "\n", encoding="utf-8")
    sink = JsonlSink(path)
    sink.open()
    sink.backup()
    sink.upsert(material_batch([MaterialRecord("demo", "A", name="new")], owned=ALL))
    stored = read_jsonl(path)[("demo", "A")]
    assert stored["stock_meters"] == 12.5, "a hand-added column is not ours to drop"
    assert stored["name"] == "new"


def test_new_rows_are_appended_after_existing_ones_and_hold_only_what_was_owned(
    tmp_path: Path,
) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(named("B", "b"))
    sink.upsert(named("A", "a"))
    rows = read_jsonl(tmp_path / "catalog.jsonl")
    assert list(rows) == [("contract", "B"), ("contract", "A")]
    assert rows[("contract", "A")] == {"source": "contract", "source_id": "A", "name": "a"}


def test_a_fully_owned_material_row_loads_back_as_a_record(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(material_batch([MaterialRecord("demo", "A", name="n")], owned=ALL))
    stored = read_jsonl(tmp_path / "catalog.jsonl")[("demo", "A")]
    assert MaterialRecord.from_dict(stored).name == "n"


def test_an_unchanged_batch_does_not_rewrite_the_file(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    batch = named("A", "a")
    sink.upsert(batch)
    path = tmp_path / "catalog.jsonl"
    before = path.stat()
    sink.upsert(batch)
    after = path.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_an_empty_batch_creates_no_file(tmp_path: Path) -> None:
    open_sink(tmp_path).upsert(contract_batch([]))
    assert not (tmp_path / "catalog.jsonl").exists()


def test_a_dry_run_creates_neither_file_nor_directory(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "deep" / "dir" / "catalog.jsonl")
    sink.open()
    sink.upsert(named("A", "a"), dry_run=True)
    assert files_in(tmp_path) == []


def test_backup_of_a_missing_file_is_an_empty_receipt(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "catalog.jsonl")
    sink.open()
    assert sink.backup() == BackupReceipt(path=None, rows_before=0)


def test_two_backups_in_one_instant_do_not_overwrite_each_other(tmp_path: Path) -> None:
    path = tmp_path / "catalog.jsonl"
    frozen = datetime(2026, 1, 1, tzinfo=UTC)
    sink = JsonlSink(path, clock=lambda: frozen)
    sink.open()
    sink.backup()
    sink.upsert(named("A", "a"))
    first = sink.backup()
    sink.upsert(named("B", "b"))
    with pytest.raises(SinkError, match="already exists"):
        sink.backup()
    assert first.path is not None
    assert list(read_jsonl(first.path)) == [("contract", "A")]


def test_a_corrupt_existing_file_is_reported_before_anything_is_changed(tmp_path: Path) -> None:
    path = tmp_path / "catalog.jsonl"
    good = json.dumps({"source": "demo", "source_id": "A"})
    path.write_text(good + "\n{not json\n", encoding="utf-8")
    sink = JsonlSink(path)
    sink.open()
    with pytest.raises(RecordFormatError, match="line 2"):
        sink.backup()
    assert path.read_text(encoding="utf-8") == good + "\n{not json\n"
    assert files_in(tmp_path) == [path], "no backup of a file we could not read"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('["a"]\n', "not a JSON object"),
        ('{"source": "s"}\n', "string 'source' and 'source_id'"),
        ('{"source": "s", "source_id": 1}\n', "string 'source' and 'source_id'"),
        ('{"source": "s", "source_id": "a"}\n{"source": "s", "source_id": "a"}\n', "duplicate key"),
    ],
)
def test_malformed_rows_are_refused(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "catalog.jsonl"
    path.write_text(content, encoding="utf-8")
    sink = JsonlSink(path)
    sink.open()
    with pytest.raises(RecordFormatError, match=message):
        sink.backup()


def test_use_before_open_is_refused(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "catalog.jsonl")
    with pytest.raises(SinkError, match="not open"):
        sink.backup()
    sink.open()
    sink.close()
    with pytest.raises(SinkError, match="not open"):
        sink.upsert(contract_batch([]))


def test_reopening_requires_a_fresh_backup(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.close()
    sink.open()
    with pytest.raises(SinkError, match="backup"):
        sink.upsert(named("A", "a"))


def test_output_is_private(tmp_path: Path) -> None:
    sink = open_sink(tmp_path)
    sink.upsert(named("A", "a"))
    assert (tmp_path / "catalog.jsonl").stat().st_mode & 0o077 == 0


def test_options_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="'path'"):
        JsonlSink.from_options({}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="unknown option"):
        JsonlSink.from_options({"path": "x", "mode": "y"}, base_dir=tmp_path)
    sink = JsonlSink.from_options({"path": "out/c.jsonl"}, base_dir=tmp_path)
    assert sink.path == tmp_path / "out" / "c.jsonl"


def test_merge_is_deterministic_for_rows_missing_a_field(tmp_path: Path) -> None:
    path = tmp_path / "catalog.jsonl"
    path.write_text(json.dumps({"source": "contract", "source_id": "A"}) + "\n", encoding="utf-8")
    sink = JsonlSink(path)
    sink.open()
    sink.backup()
    sink.upsert(contract_batch([contract_row("A", name="n", weight=1.5)]))
    keys = list(read_jsonl(path)[("contract", "A")])
    assert keys == ["source", "source_id", *sorted(CONTRACT_KINDS)]


# --- the advisory lock ---------------------------------------------------------------------------

HOLD_THE_LOCK = """
import sys
from catalog_sync.sinks.jsonl import JsonlSink
from catalog_sync.sinks.lifecycle import LockedFileSink
sink = JsonlSink(__import__("pathlib").Path(sys.argv[1]))
sink.open()
sink.backup()
print("locked", flush=True)
sys.stdin.readline()
"""


def test_a_second_load_into_the_same_file_is_refused_while_the_first_is_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.jsonl"
    first = JsonlSink(path)
    first.open()
    first.backup()
    second = JsonlSink(path)
    second.open()
    with pytest.raises(SinkError, match="another catalog-sync process"):
        second.backup()
    first.close()
    second.backup()  # the lock is free again, and the refused sink was not left half-open


def test_the_lock_is_released_by_close_and_by_the_death_of_its_holder(tmp_path: Path) -> None:
    path = tmp_path / "catalog.jsonl"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_THE_LOCK, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        contender = JsonlSink(path)
        contender.open()
        with pytest.raises(SinkError, match="another catalog-sync process"):
            contender.backup()
    finally:
        holder.kill()  # no clean shutdown: the operating system must free the lock
        holder.wait()
    contender.backup()


def test_a_dry_run_takes_no_lock_and_leaves_no_lock_file(tmp_path: Path) -> None:
    path = tmp_path / "catalog.jsonl"
    holder = JsonlSink(path)
    holder.open()
    holder.backup()
    dry = JsonlSink(path)
    dry.open()
    assert dry.upsert(named("A", "a"), dry_run=True).inserted == 1  # reading is always allowed
    other = tmp_path / "other"
    quiet = JsonlSink(other / "catalog.jsonl")
    quiet.open()
    quiet.upsert(named("A", "a"), dry_run=True)
    assert not other.exists(), "a dry run into a store that does not exist creates nothing"


def test_the_refusal_comes_from_the_lock_and_from_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old behaviour (no lock) must let the second writer through, or the tests above would
    pass for some other reason."""
    monkeypatch.setattr(LockedFileSink, "_acquire_lock", lambda _self: None)
    path = tmp_path / "catalog.jsonl"
    first, second = JsonlSink(path), JsonlSink(path)
    first.open()
    first.backup()
    second.open()
    second.backup()  # raises SinkError if anything but the lock was refusing it
