"""Normalising a stored run offline, including incremental runs whose detail lives in older runs."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError, RecordFormatError
from catalog_sync.normalize.mapping import load_mapping
from catalog_sync.normalize.output import (
    from_run,
    meta_path,
    read_normalized,
    write_normalized,
)
from catalog_sync.normalize.run import find_run, normalize_run
from catalog_sync.puller import Puller
from catalog_sync.snapshot import RunSnapshot
from catalog_sync.sources.replay import ReplaySource
from catalog_sync.state import StateStore
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.doubles import Spy, make_scripted_mock
from tests.fixtures import mock_snapshot
from tests.fixtures.mock_saas import picked

ITEM_COUNT = mock_snapshot.ITEM_COUNT
DEFAULT_MAPPING = mock_snapshot.MAPPING
SOURCE = mock_snapshot.SOURCE
CLOCK_TIME = datetime(2026, 4, 1, tzinfo=UTC)


def clock() -> datetime:
    return CLOCK_TIME


def pull_replay(store: StateStore, runs_root: Path) -> str:
    puller = Puller(ReplaySource(RunSnapshot.open(MOCK_SNAPSHOT_DIR)), store, runs_root)
    return puller.pull().run_id


def normalize_latest(store: StateStore, runs_root: Path, mapping: Path = DEFAULT_MAPPING):  # type: ignore[no-untyped-def]
    run = find_run(store, [SOURCE], "latest")
    return normalize_run(store, runs_root, run, load_mapping(mapping), clock=clock)


def by_id(normalized) -> dict:  # type: ignore[no-untyped-def]
    return {record.source_id: record for record in normalized.records}


# --- a complete run -----------------------------------------------------------------------------


def test_a_complete_run_normalises_every_item_with_detail_and_pictures(
    store: StateStore, runs_root: Path
) -> None:
    run_id = pull_replay(store, runs_root)
    normalized = normalize_latest(store, runs_root)
    assert len(normalized.records) == ITEM_COUNT and normalized.skipped == ()
    assert normalized.is_full_snapshot
    material = mock_snapshot.CLEAN
    record = by_id(normalized)[material.id]
    expected = mock_snapshot.expected_record(material)
    assert picked(record.to_dict(), expected) == expected, "every field the dataset predicts"
    assert record.code == material.code
    assert record.weight_gsm == float(material.weight_gsm)
    assert [image.role for image in record.images] == ["thumb", "swatch"]
    assert record.images[0].local_path == f"assets/{material.id}+thumb.bin"
    assert record.raw_ref == f"{run_id}:{material.id}"
    assert record.normalized_at == CLOCK_TIME


def test_the_documented_gaps_in_the_synthetic_library_become_issues_not_values(
    store: StateStore, runs_root: Path
) -> None:
    pull_replay(store, runs_root)
    records = by_id(normalize_latest(store, runs_root))
    for material in mock_snapshot.ZERO_WEIGHT:  # the source writes "0 gsm" for "not filled in"
        zero_weight = records[material.id]
        assert zero_weight.weight_gsm is None
        assert "weight_gsm:source_zero" in zero_weight.issues
    for material in mock_snapshot.UNKNOWN_FIBRE:  # "60%PES/40%ZZ": ZZ is in no code table
        unknown_code = records[material.id]
        assert [c.fiber is None for c in unknown_code.composition] == [False, True]
        assert "composition:unknown_code:ZZ" in unknown_code.issues
    clean = records[mock_snapshot.CLEAN.id]
    assert clean.issues == ()


# --- incremental runs ---------------------------------------------------------------------------


def test_latest_resolves_unchanged_items_through_the_run_that_last_pulled_them(
    store: StateStore, runs_root: Path
) -> None:
    first = pull_replay(store, runs_root)
    second = pull_replay(store, runs_root)
    assert RunSnapshot.open(runs_root / second).names("detail") == [], "the delta run has no detail"
    assert RunSnapshot.open(runs_root / second).names("list") != []

    normalized = normalize_latest(store, runs_root)
    assert normalized.run.run_id == second
    assert len(normalized.records) == ITEM_COUNT
    material = mock_snapshot.CLEAN
    record = by_id(normalized)[material.id]
    assert record.weight_gsm == float(material.weight_gsm), "detail came from the first run"
    assert record.images[0].local_path is not None, "so did the picture"
    assert record.raw_ref == f"{first}:{material.id}"


def test_a_changed_item_is_read_from_the_run_that_fetched_it(
    store: StateStore, runs_root: Path
) -> None:
    source = make_scripted_mock(3)
    Puller(source, store, runs_root).pull()
    changed = source.items[1]
    source.items[1] = replace(
        changed, version="9999999999", payload={**changed.payload, "name": "Renamed"}
    )
    source.details[changed.source_id] = {**source.details[changed.source_id], "weight": "222 gsm"}
    second = Puller(source, store, runs_root).pull().run_id

    normalized = normalize_latest(store, runs_root)
    records = by_id(normalized)
    first_id, changed_id = source.items[0].source_id, changed.source_id
    assert records[changed_id].weight_gsm == 222.0
    assert records[changed_id].raw_ref == f"{second}:{changed_id}"
    assert records[first_id].weight_gsm == float(mock_snapshot.MATERIALS[0].weight_gsm)
    assert records[first_id].raw_ref != records[changed_id].raw_ref


def test_a_folder_move_without_a_version_change_shows_in_the_latest_listing(
    store: StateStore, runs_root: Path
) -> None:
    source = make_scripted_mock(2)
    Puller(source, store, runs_root).pull()
    moved = source.items[0]
    source.items[0] = replace(moved, context={"folder_paths": ["Somewhere Else"]})
    Puller(source, store, runs_root).pull()
    record = by_id(normalize_latest(store, runs_root))[moved.source_id]
    assert record.folder_paths == ("Somewhere Else",)
    assert record.weight_gsm == float(mock_snapshot.MATERIALS[0].weight_gsm), (
        "detail still comes from the earlier complete pull"
    )


def test_an_explicit_older_run_whose_items_have_since_changed_skips_those_items(
    store: StateStore, runs_root: Path
) -> None:
    source = make_scripted_mock(2)
    Puller(source, store, runs_root).pull()
    second = Puller(source, store, runs_root).pull().run_id  # everything skipped as unchanged
    changed = source.items[0]
    source.items[0] = replace(changed, version="v-new")
    Puller(source, store, runs_root).pull()  # item 0 re-pulled: item_versions moves on

    older = normalize_run(
        store, runs_root, store.get_run(second), load_mapping(DEFAULT_MAPPING), clock=clock
    )
    assert [s.source_id for s in older.skipped] == [changed.source_id]
    assert "no longer" in older.skipped[0].reason
    assert [r.source_id for r in older.records] == [source.items[1].source_id]
    assert not older.is_full_snapshot


# --- incomplete items and unusable caches -------------------------------------------------------


def test_an_item_that_did_not_finish_pulling_is_skipped_not_normalised_from_the_listing_alone(
    store: StateStore, runs_root: Path
) -> None:
    def fail_one(source_id: str) -> None:
        from catalog_sync.errors import ItemFailed

        if source_id == mock_snapshot.MATERIALS[1].id:
            raise ItemFailed(source_id, "HTTP 404")

    Puller(Spy(make_scripted_mock(3), before_detail=fail_one), store, runs_root).pull()
    normalized = normalize_latest(store, runs_root)
    assert [s.source_id for s in normalized.skipped] == [mock_snapshot.MATERIALS[1].id]
    assert "detail" in normalized.skipped[0].reason
    assert len(normalized.records) == 2
    assert not normalized.is_full_snapshot, "a skipped item must not be reported as gone"


def test_a_run_whose_listing_stopped_early_is_not_a_full_snapshot(
    store: StateStore, runs_root: Path
) -> None:
    Puller(make_scripted_mock(4), store, runs_root).pull(max_items=2)
    normalized = normalize_latest(store, runs_root)
    assert len(normalized.records) == 2
    assert not normalized.is_full_snapshot


def test_a_purged_holder_run_skips_its_items_and_names_the_cause(
    store: StateStore, runs_root: Path
) -> None:
    first = pull_replay(store, runs_root)
    pull_replay(store, runs_root)
    shutil.rmtree(runs_root / first)
    normalized = normalize_latest(store, runs_root)
    assert normalized.records == ()
    assert len(normalized.skipped) == ITEM_COUNT
    assert first in normalized.skipped[0].reason


# --- refusals -----------------------------------------------------------------------------------


def test_no_run_to_normalise_is_a_clear_error(store: StateStore) -> None:
    with pytest.raises(ConfigError, match=r"no run of mock\.materials to normalise"):
        find_run(store, [SOURCE], "latest")


def test_latest_tries_each_name_in_order(store: StateStore, runs_root: Path) -> None:
    run_id = pull_replay(store, runs_root)
    assert find_run(store, ["demo-alias", SOURCE], "latest").run_id == run_id
    assert find_run(store, ["whatever"], run_id).run_id == run_id


def test_latest_skips_a_run_that_is_still_running(store: StateStore, runs_root: Path) -> None:
    """A run being written has a half-written snapshot; reading it as `latest` gives wrong data."""
    finished = pull_replay(store, runs_root)
    store.create_run("zzz-live", SOURCE)  # sorts after every real run id
    assert store.latest_run(SOURCE) is not None
    assert find_run(store, [SOURCE], "latest").run_id == finished


def test_latest_explains_when_the_only_run_is_still_running(store: StateStore) -> None:
    store.create_run("live-run", SOURCE)
    with pytest.raises(ConfigError, match=r"live-run .*still running.*--run"):
        find_run(store, [SOURCE], "latest")


def test_a_running_run_can_still_be_named_explicitly(store: StateStore) -> None:
    store.create_run("live-run", SOURCE)
    assert find_run(store, [SOURCE], "live-run").run_id == "live-run"


def test_a_mapping_for_another_source_is_refused(
    store: StateStore, runs_root: Path, tmp_path: Path
) -> None:
    pull_replay(store, runs_root)
    other = tmp_path / "other.yaml"
    other.write_text(
        DEFAULT_MAPPING.read_text(encoding="utf-8").replace(
            "source: mock.materials", "source: other"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="written for source 'other'"):
        normalize_latest(store, runs_root, other)


def test_a_changed_mapping_renormalises_the_same_cache_with_no_source_at_all(
    store: StateStore, runs_root: Path, tmp_path: Path
) -> None:
    pull_replay(store, runs_root)
    material = mock_snapshot.CLEAN
    before = by_id(normalize_latest(store, runs_root))[material.id]
    changed = tmp_path / "changed.yaml"
    changed.write_text(
        DEFAULT_MAPPING.read_text(encoding="utf-8").replace(
            "    from: [list.name, detail.name]", "    from: [detail.code]"
        ),
        encoding="utf-8",
    )
    after = by_id(normalize_latest(store, runs_root, changed))[material.id]
    assert before.name == material.name
    assert after.name == material.code, "the new rule took effect without any source"


# --- the normalised file ------------------------------------------------------------------------


def test_the_file_and_its_sidecar_round_trip(
    store: StateStore, runs_root: Path, tmp_path: Path
) -> None:
    pull_replay(store, runs_root)
    spec = load_mapping(DEFAULT_MAPPING)
    normalized = normalize_latest(store, runs_root)
    out = tmp_path / "out" / "records.jsonl"
    write_normalized(out, from_run(normalized, spec))

    loaded = read_normalized(out)
    assert loaded.records == normalized.records
    assert loaded.keep_existing_if_null == {"images"}
    assert loaded.is_full_snapshot
    assert (loaded.source, loaded.mapping_digest) == (spec.source, spec.digest)
    assert "normalized_at" not in loaded.owned, "re-normalising must not look like a change"
    assert {"issues", "source_version", "raw_ref", "fetched_at"} <= loaded.owned
    assert {"code", "composition", "images"} <= loaded.owned
    assert out.stat().st_mode & 0o077 == 0 and meta_path(out).stat().st_mode & 0o077 == 0


def test_load_refuses_a_file_without_its_sidecar_or_with_a_wrong_count(
    store: StateStore, runs_root: Path, tmp_path: Path
) -> None:
    pull_replay(store, runs_root)
    out = tmp_path / "records.jsonl"
    write_normalized(
        out, from_run(normalize_latest(store, runs_root), load_mapping(DEFAULT_MAPPING))
    )

    lines = out.read_text(encoding="utf-8").splitlines()
    out.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(RecordFormatError, match="edited or truncated"):
        read_normalized(out)

    meta_path(out).unlink()
    with pytest.raises(RecordFormatError, match="metadata"):
        read_normalized(out)


def test_a_sidecar_from_another_version_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "records.jsonl"
    out.write_text("", encoding="utf-8")
    meta_path(out).write_text(json.dumps({"format": 99}), encoding="utf-8")
    with pytest.raises(RecordFormatError, match="not metadata written by this version"):
        read_normalized(out)


def test_a_missing_records_file_is_a_clear_error_not_a_traceback(tmp_path: Path) -> None:
    out = tmp_path / "records.jsonl"
    meta_path(out).write_text(
        json.dumps(
            {
                "format": 1,
                "source": "s",
                "run_id": "r",
                "mapping_sha256": "0" * 64,
                "record_count": 0,
                "owned": [],
                "is_full_snapshot": False,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RecordFormatError, match=r"cannot read records\.jsonl"):
        read_normalized(out)
