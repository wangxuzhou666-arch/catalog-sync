"""End-to-end pulls of the committed synthetic snapshot through ReplaySource and Puller."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from catalog_sync.errors import AuthExpired, ConfigError
from catalog_sync.puller import Puller
from catalog_sync.snapshot import RunSnapshot
from catalog_sync.sources.base import AssetRef, Inspectable
from catalog_sync.sources.replay import ReplaySource
from catalog_sync.state import RunStatus, Stage, StageStatus, StateStore
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.doubles import SimulatedCrash, Spy, make_scripted_mock
from tests.fixtures import mock_snapshot

ITEM_COUNT = mock_snapshot.ITEM_COUNT


# The order the snapshot lists its items in (the Mock's folder walk: a folder's own items before
# its sub-folders'), which is the order a pull handles them in.
LISTED = RunSnapshot.open(MOCK_SNAPSHOT_DIR).item_ids()


def material_id(index: int) -> str:
    """The id of the index-th item a pull handles."""
    return LISTED[index]


def replay_spy() -> Spy:
    return Spy(ReplaySource(RunSnapshot.open(MOCK_SNAPSHOT_DIR)))


def assert_matches_original(snapshot: RunSnapshot) -> None:
    original = RunSnapshot.open(MOCK_SNAPSHOT_DIR)
    assert snapshot.item_ids() == original.item_ids()
    for source_id in original.item_ids():
        assert snapshot.bundle(source_id) == original.bundle(source_id)
    assert snapshot.verify() == []


def test_a_replay_pull_copies_the_whole_snapshot(store: StateStore, runs_root: Path) -> None:
    spy = replay_spy()
    report = Puller(spy, store, runs_root).pull()

    assert report.status is RunStatus.COMPLETE
    assert report.source == mock_snapshot.SOURCE
    assert report.item_count == ITEM_COUNT
    assert report.stage_counts[Stage.DETAIL] == {StageStatus.DONE: ITEM_COUNT}
    assert (report.detail_fetches, report.asset_fetches) == (ITEM_COUNT, 2 * ITEM_COUNT)
    assert_matches_original(RunSnapshot.open(runs_root / report.run_id))
    assert [r.run_id for r in store.list_runs()] == [report.run_id]


def test_resume_after_a_crash_between_items_never_refetches_finished_work(
    store: StateStore, runs_root: Path
) -> None:
    crash_index = 5
    spy = replay_spy()

    def crash_on_item_five(source_id: str) -> None:
        if source_id == material_id(crash_index):
            raise SimulatedCrash

    spy.before_detail = crash_on_item_five
    puller = Puller(spy, store, runs_root)
    with pytest.raises(SimulatedCrash):
        puller.pull()

    (interrupted,) = store.list_runs()
    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.list_complete, "the listing finished before the detail stage crashed"
    assert spy.asset_calls == [], "assets come after details"

    spy.before_detail = None
    report = puller.pull(resume=interrupted.run_id)

    assert report.status is RunStatus.COMPLETE
    detail_calls = Counter(spy.detail_calls)
    expected = {material_id(i): 1 for i in range(ITEM_COUNT)}
    expected[material_id(crash_index)] = 2  # the call that crashed, then the retry
    assert detail_calls == expected
    assert Counter(spy.asset_calls).most_common(1)[0][1] == 1, "every asset fetched exactly once"
    assert report.detail_fetches == ITEM_COUNT - crash_index, (
        "the resumed invocation only did the rest"
    )
    assert_matches_original(RunSnapshot.open(runs_root / interrupted.run_id))


def test_resume_after_a_crash_inside_an_item_keeps_its_finished_assets(
    store: StateStore, runs_root: Path
) -> None:
    spy = replay_spy()
    two_asset_item = material_id(3)  # every material has two pictures: thumb, swatch

    def crash_on_second_asset(ref: AssetRef) -> None:
        if (ref.source_id, ref.role) == (two_asset_item, "swatch"):
            raise SimulatedCrash

    spy.before_asset = crash_on_second_asset
    puller = Puller(spy, store, runs_root)
    with pytest.raises(SimulatedCrash):
        puller.pull()
    (interrupted,) = store.list_runs()

    spy.before_asset = None
    report = puller.pull(resume=interrupted.run_id)

    assert report.status is RunStatus.COMPLETE
    asset_calls = Counter(spy.asset_calls)
    assert asset_calls[(two_asset_item, "thumb")] == 1, "fetched before the crash, not again"
    assert asset_calls[(two_asset_item, "swatch")] == 2, "crashed once, then retried"
    assert max(asset_calls.values()) == 2
    assert sum(1 for count in asset_calls.values() if count == 2) == 1
    assert_matches_original(RunSnapshot.open(runs_root / interrupted.run_id))


def test_an_expired_credential_stops_the_run_and_it_can_be_continued(
    store: StateStore, runs_root: Path
) -> None:
    spy = replay_spy()

    def expire_at_item_two(source_id: str) -> None:
        if source_id == material_id(2):
            raise AuthExpired("token rejected")

    spy.before_detail = expire_at_item_two
    puller = Puller(spy, store, runs_root)
    with pytest.raises(AuthExpired):
        puller.pull()
    (stopped,) = store.list_runs()
    assert stopped.status is RunStatus.INTERRUPTED
    cp = store.checkpoint(stopped.run_id)
    assert cp.pending(Stage.DETAIL)[0] == material_id(2)

    spy.before_detail = None
    assert puller.pull(resume=stopped.run_id).status is RunStatus.COMPLETE


def test_a_second_pull_of_unchanged_data_fetches_nothing(
    store: StateStore, runs_root: Path
) -> None:
    spy = replay_spy()
    puller = Puller(spy, store, runs_root)
    first = puller.pull()
    spy.detail_calls.clear()
    spy.asset_calls.clear()

    second = puller.pull()

    assert second.run_id != first.run_id
    assert (second.detail_fetches, second.asset_fetches) == (0, 0)
    assert spy.detail_calls == [] and spy.asset_calls == []
    assert second.stage_counts[Stage.DETAIL] == {StageStatus.SKIPPED: ITEM_COUNT}

    forced = puller.pull(incremental=False)
    assert forced.detail_fetches == ITEM_COUNT


def test_resume_of_a_finished_run_is_a_no_op(store: StateStore, runs_root: Path) -> None:
    spy = replay_spy()
    puller = Puller(spy, store, runs_root)
    first = puller.pull()
    spy.detail_calls.clear()
    again = puller.pull(resume=first.run_id)
    assert again.status is RunStatus.COMPLETE
    assert spy.detail_calls == []


def test_resume_refuses_a_run_of_another_source(store: StateStore, runs_root: Path) -> None:
    first = Puller(replay_spy(), store, runs_root).pull()
    other = make_scripted_mock(1)
    other.name = "something.else"
    with pytest.raises(ConfigError, match="belongs to source"):
        Puller(other, store, runs_root).pull(resume=first.run_id)


def test_the_replay_source_rejects_bad_options(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="'path'"):
        ReplaySource.from_options({}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="unknown option"):
        ReplaySource.from_options({"path": "x", "extra": "y"}, base_dir=tmp_path)


def test_inspect_summarises_the_snapshot_without_pulling_anything() -> None:
    source = ReplaySource(RunSnapshot.open(MOCK_SNAPSHOT_DIR))
    assert isinstance(source, Inspectable)

    report = source.inspect(None)

    assert report.total == ITEM_COUNT
    assert report.folders == ("Demo Knit", "Demo Woven")
    assert "synthetic" in report.notes[0]


def test_inspect_can_look_into_one_folder_of_the_snapshot() -> None:
    source = ReplaySource(RunSnapshot.open(MOCK_SNAPSHOT_DIR))
    twills = source.inspect("Demo Woven/Twills")
    jersey = source.inspect("Demo Knit/Jersey")
    assert (twills.total, jersey.total) == (3, 2)
    assert twills.folders == ()
    woven = source.inspect("Demo Woven")
    assert woven.total == 9, "the folder's own items and those of its two sub-folders"
    assert woven.folders == ("Denim", "Twills")
    assert source.inspect("No Such Folder").total == 0
