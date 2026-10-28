from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catalog_sync.errors import CatalogSyncError
from catalog_sync.state import RunStatus, Stage, StageStatus, StateStore, StoredPull


def test_frontier_survives_closing_and_reopening_the_database(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    with StateStore(path) as store:
        store.create_run("r1", "demo")
        store.checkpoint("r1").set_frontier("walk", {"queue": ["a", "b"], "depth": 2})
    with StateStore(path) as store:
        assert store.checkpoint("r1").get_frontier("walk") == {"queue": ["a", "b"], "depth": 2}
        assert store.checkpoint("r1").get_frontier("other") is None


def test_frontiers_are_per_run(store: StateStore) -> None:
    store.create_run("r1", "demo")
    store.create_run("r2", "demo")
    store.checkpoint("r1").set_frontier("k", 5)
    assert store.checkpoint("r2").get_frontier("k") is None


def test_pending_lists_unfinished_and_failed_items_in_listing_order(store: StateStore) -> None:
    store.create_run("r1", "demo")
    cp = store.checkpoint("r1")
    for source_id in ("c", "a", "b", "d"):
        cp.add_item(source_id, "v", detail=StageStatus.PENDING, assets=StageStatus.PENDING)
    cp.mark("a", Stage.DETAIL, StageStatus.DONE)
    cp.mark("b", Stage.DETAIL, StageStatus.FAILED, "boom")
    cp.mark("d", Stage.DETAIL, StageStatus.SKIPPED)
    assert cp.pending(Stage.DETAIL) == ["c", "b"], "ordinal order, failed ones are retried"
    assert cp.failures()[0].error == "boom"


def test_pending_after_requires_the_earlier_stage_to_have_succeeded(store: StateStore) -> None:
    store.create_run("r1", "demo")
    cp = store.checkpoint("r1")
    for source_id in ("done", "skipped", "failed", "pending"):
        cp.add_item(source_id, "v", detail=StageStatus.PENDING, assets=StageStatus.PENDING)
    cp.mark("done", Stage.DETAIL, StageStatus.DONE)
    cp.mark("skipped", Stage.DETAIL, StageStatus.SKIPPED)
    cp.mark("failed", Stage.DETAIL, StageStatus.FAILED, "x")
    assert cp.pending(Stage.ASSETS) == ["done", "skipped", "failed", "pending"]
    assert cp.pending(Stage.ASSETS, after=Stage.DETAIL) == ["done", "skipped"]


def test_item_row_reports_whether_every_stage_finished(store: StateStore) -> None:
    store.create_run("r1", "demo")
    cp = store.checkpoint("r1")
    cp.add_item("a", "v1", detail=StageStatus.PENDING, assets=StageStatus.PENDING)
    assert not cp.item("a").is_fully_pulled
    cp.mark("a", Stage.DETAIL, StageStatus.DONE)
    cp.mark("a", Stage.ASSETS, StageStatus.SKIPPED)
    assert cp.item("a").is_fully_pulled
    assert cp.item("a").version == "v1"


def test_mark_done_records_the_version_only_when_it_finishes_the_item(store: StateStore) -> None:
    store.create_run("r1", "demo")
    cp = store.checkpoint("r1")
    cp.add_item("a", "v1", detail=StageStatus.PENDING, assets=StageStatus.PENDING)
    cp.mark_done("a", Stage.DETAIL, source="demo")
    assert store.stored_version("demo", "a") is None, "assets are still pending"
    cp.mark_done("a", Stage.ASSETS, source="demo")
    assert store.stored_pull("demo", "a") == StoredPull("v1", "r1")


def test_a_failure_while_recording_the_version_leaves_the_stage_unmarked(
    store: StateStore, tmp_path: Path
) -> None:
    """One transaction: a kill between 'stage done' and 'version stored' used to leave the item
    looking pulled (all stages done) while the next incremental run still saw the old version."""
    store.create_run("r1", "demo")
    cp = store.checkpoint("r1")
    cp.add_item("a", "v1", detail=StageStatus.DONE, assets=StageStatus.PENDING)
    conn = sqlite3.connect(tmp_path / "state.sqlite")  # the `store` fixture's file
    try:
        conn.execute(
            "CREATE TRIGGER no_versions BEFORE INSERT ON item_versions "
            "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.DatabaseError, match="disk full"):
        cp.mark_done("a", Stage.ASSETS, source="demo")
    assert cp.item("a").stages[Stage.ASSETS] is StageStatus.PENDING
    assert cp.pending(Stage.ASSETS) == ["a"], "the unit is retried, not lost"


def test_unknown_items_are_errors(store: StateStore) -> None:
    store.create_run("r1", "demo")
    cp = store.checkpoint("r1")
    with pytest.raises(CatalogSyncError, match="no item"):
        cp.item("ghost")
    with pytest.raises(CatalogSyncError, match="no item"):
        cp.mark("ghost", Stage.DETAIL, StageStatus.DONE)
    with pytest.raises(CatalogSyncError, match="no such run"):
        store.get_run("ghost")


def test_versions_are_global_per_source_and_item(store: StateStore) -> None:
    assert store.stored_version("demo", "a") is None
    store.record_version("demo", "a", "v1", "r1")
    store.record_version("demo", "a", "v2", "r2")
    store.record_version("other", "a", "x", "r3")
    assert store.stored_version("demo", "a") == "v2"
    assert store.stored_version("other", "a") == "x"


def test_runs_list_newest_first_and_status_transitions(tmp_path: Path) -> None:
    moments = iter(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=n) for n in range(100))
    with StateStore(tmp_path / "s.sqlite", clock=lambda: next(moments)) as store:
        store.create_run("old", "demo")
        store.create_run("new", "demo")
        assert [r.run_id for r in store.list_runs()] == ["new", "old"]
        latest = store.latest_run("demo")
        assert latest is not None
        assert latest.run_id == "new"
        assert store.latest_run("nothing") is None
        store.finish_run("new", RunStatus.FUSED)
        assert store.get_run("new").status is RunStatus.FUSED
        assert store.get_run("new").finished_at is not None
        store.reopen_run("new")
        assert store.get_run("new").status is RunStatus.RUNNING
        assert store.get_run("new").finished_at is None


def test_a_database_from_another_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    with StateStore(path):
        pass
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    with pytest.raises(CatalogSyncError, match="schema version 99"):
        StateStore(path)


def test_foreign_keys_are_enforced(store: StateStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.checkpoint("no-such-run").add_item(
            "a", "v", detail=StageStatus.PENDING, assets=StageStatus.PENDING
        )
