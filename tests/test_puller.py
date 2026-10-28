from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError, FuseTripped, ItemFailed
from catalog_sync.puller import Puller, effective_version, merge_context
from catalog_sync.snapshot import RunSnapshot
from catalog_sync.sources.base import AssetRef, ListedItem
from catalog_sync.state import RunStatus, Stage, StageStatus, StateStore
from tests.doubles import ScriptedSource, SimulatedCrash, Spy, make_scripted
from tests.fixtures import sample_items


def ids(count: int) -> list[str]:
    return [sample_items.item_id(i) for i in range(count)]


def fail_details_for(failing: set[str]) -> Callable[[str], None]:
    def hook(source_id: str) -> None:
        if source_id in failing:
            raise ItemFailed(source_id, "HTTP 404")

    return hook


def test_every_stage_runs_for_every_item_and_versions_are_recorded(
    store: StateStore, runs_root: Path
) -> None:
    report = Puller(make_scripted(6), store, runs_root).pull()
    assert report.status is RunStatus.COMPLETE
    assert report.stage_counts[Stage.LIST] == {StageStatus.DONE: 6}
    assert report.stage_counts[Stage.ASSETS] == {StageStatus.DONE: 6}
    assert store.stored_version(sample_items.SOURCE_NAME, sample_items.item_id(0)) == str(
        sample_items.BASE_TIMESTAMP
    )


def test_one_failing_item_is_recorded_and_the_rest_continue(
    store: StateStore, runs_root: Path
) -> None:
    spy = Spy(make_scripted(6), before_detail=fail_details_for({sample_items.item_id(2)}))
    report = Puller(spy, store, runs_root).pull()

    assert report.status is RunStatus.COMPLETE_WITH_FAILURES
    (failure,) = report.failures
    assert (failure.source_id, failure.stage) == (sample_items.item_id(2), Stage.DETAIL)
    assert "HTTP 404" in failure.error
    assert report.stage_counts[Stage.DETAIL] == {StageStatus.DONE: 5, StageStatus.FAILED: 1}
    assert report.stage_counts[Stage.ASSETS] == {StageStatus.DONE: 5, StageStatus.PENDING: 1}, (
        "an item whose detail failed is retried as a whole, its assets wait"
    )
    assert (sample_items.item_id(2), "thumb") not in spy.asset_calls
    assert store.stored_version(sample_items.SOURCE_NAME, sample_items.item_id(2)) is None
    assert store.stored_version(sample_items.SOURCE_NAME, sample_items.item_id(3)) is not None


def test_a_failed_item_is_retried_on_resume_and_then_completes(
    store: StateStore, runs_root: Path
) -> None:
    spy = Spy(make_scripted(4), before_detail=fail_details_for({sample_items.item_id(1)}))
    puller = Puller(spy, store, runs_root)
    first = puller.pull()
    spy.before_detail = None

    second = puller.pull(resume=first.run_id)

    assert second.status is RunStatus.COMPLETE
    assert spy.detail_calls.count(sample_items.item_id(1)) == 2
    assert spy.detail_calls.count(sample_items.item_id(0)) == 1
    assert store.stored_version(sample_items.SOURCE_NAME, sample_items.item_id(1)) is not None


def test_consecutive_failures_trip_the_fuse_and_nothing_more_is_fetched(
    store: StateStore, runs_root: Path
) -> None:
    spy = Spy(make_scripted(10), before_detail=fail_details_for(set(ids(10))))
    puller = Puller(spy, store, runs_root, max_consecutive_failures=3)

    with pytest.raises(FuseTripped, match="3 consecutive detail failures") as raised:
        puller.pull()

    assert len(spy.detail_calls) == 3, "the fuse stops the run, it does not finish the stage"
    (fused,) = store.list_runs()
    assert fused.status is RunStatus.FUSED
    assert fused.run_id in str(raised.value), "the message names the run to resume"
    assert FuseTripped.exit_code == 3

    spy.before_detail = None
    resumed = puller.pull(resume=fused.run_id)
    assert resumed.status is RunStatus.COMPLETE
    assert resumed.stage_counts[Stage.DETAIL] == {StageStatus.DONE: 10}


def test_failures_that_are_not_consecutive_do_not_trip_the_fuse(
    store: StateStore, runs_root: Path
) -> None:
    flaky = {sample_items.item_id(i) for i in (0, 2, 4, 6)}
    spy = Spy(make_scripted(8), before_detail=fail_details_for(flaky))
    report = Puller(spy, store, runs_root, max_consecutive_failures=2).pull()
    assert report.status is RunStatus.COMPLETE_WITH_FAILURES
    assert len(report.failures) == 4


def test_the_fuse_counts_per_stage(store: StateStore, runs_root: Path) -> None:
    source = make_scripted(6)
    spy = Spy(source, before_detail=fail_details_for(set(ids(2))))  # two detail failures
    for index in (2, 3):  # then two asset failures, with the fuse set to 3
        source.asset_roles[sample_items.item_id(index)] = ("thumb",)
        del source.assets[(sample_items.item_id(index), "thumb")]
    report = Puller(spy, store, runs_root, max_consecutive_failures=3).pull()
    assert report.status is RunStatus.COMPLETE_WITH_FAILURES
    assert len(report.failures) == 4


def test_a_changed_item_is_refetched_and_the_others_are_not(
    store: StateStore, runs_root: Path
) -> None:
    source = make_scripted(5)
    spy = Spy(source)
    puller = Puller(spy, store, runs_root)
    puller.pull()
    spy.detail_calls.clear()
    spy.asset_calls.clear()

    changed = source.items[3]
    source.items[3] = ListedItem(changed.source_id, "newer", changed.payload, changed.context)
    second = puller.pull()

    assert spy.detail_calls == [sample_items.item_id(3)]
    assert {sid for sid, _ in spy.asset_calls} == {sample_items.item_id(3)}
    assert second.stage_counts[Stage.DETAIL] == {StageStatus.DONE: 1, StageStatus.SKIPPED: 4}
    assert store.stored_version(sample_items.SOURCE_NAME, sample_items.item_id(3)) == "newer"


def test_without_a_source_version_a_changed_listing_row_still_counts_as_changed(
    store: StateStore, runs_root: Path
) -> None:
    source = make_scripted(3)
    source.items = [ListedItem(i.source_id, None, i.payload, i.context) for i in source.items]
    spy = Spy(source)
    puller = Puller(spy, store, runs_root)
    puller.pull()
    spy.detail_calls.clear()

    assert puller.pull().detail_fetches == 0, "identical rows hash identically"

    first = source.items[0]
    source.items[0] = ListedItem(
        first.source_id, None, {**first.payload, "name": "Renamed"}, first.context
    )
    puller.pull()
    assert spy.detail_calls == [sample_items.item_id(0)]


def test_a_half_pulled_item_is_not_recorded_as_pulled(store: StateStore, runs_root: Path) -> None:
    source = make_scripted(3)
    spy = Spy(source)
    puller = Puller(spy, store, runs_root)
    first = puller.pull(stages=[Stage.LIST, Stage.DETAIL])
    assert first.status is RunStatus.COMPLETE
    assert store.stored_version(sample_items.SOURCE_NAME, sample_items.item_id(0)) is None, (
        "assets are still missing, so the next incremental run must not skip this item"
    )
    puller.pull(resume=first.run_id, stages=[Stage.ASSETS])
    assert store.stored_version(sample_items.SOURCE_NAME, sample_items.item_id(0)) is not None


def test_stages_can_be_run_one_at_a_time(store: StateStore, runs_root: Path) -> None:
    spy = Spy(make_scripted(4))
    puller = Puller(spy, store, runs_root)
    listed = puller.pull(stages=[Stage.LIST])
    assert (spy.detail_calls, spy.asset_calls) == ([], [])
    assert listed.stage_counts[Stage.DETAIL] == {StageStatus.PENDING: 4}

    puller.pull(resume=listed.run_id, stages=[Stage.DETAIL])
    assert len(spy.detail_calls) == 4 and spy.asset_calls == []

    final = puller.pull(resume=listed.run_id, stages=[Stage.ASSETS])
    assert final.stage_counts[Stage.ASSETS] == {StageStatus.DONE: 4}


def test_a_new_run_must_start_with_the_list_stage(store: StateStore, runs_root: Path) -> None:
    with pytest.raises(ConfigError, match="include the list stage"):
        Puller(make_scripted(2), store, runs_root).pull(stages=[Stage.DETAIL])


def test_max_items_stops_listing_and_a_resume_finishes_the_job(
    store: StateStore, runs_root: Path
) -> None:
    spy = Spy(make_scripted(12))
    puller = Puller(spy, store, runs_root)

    partial = puller.pull(max_items=5)
    assert partial.is_truncated
    assert partial.item_count == 5
    assert not store.get_run(partial.run_id).list_complete

    rest = puller.pull(resume=partial.run_id)
    assert not rest.is_truncated
    assert rest.item_count == 12
    assert store.get_run(partial.run_id).list_complete
    assert sorted(spy.detail_calls) == sorted(ids(12)), "no item fetched twice across the two calls"


def test_a_crash_while_listing_resumes_from_the_frontier(
    store: StateStore, runs_root: Path
) -> None:
    source = make_scripted(10)
    source.crash_before_index = 4
    puller = Puller(source, store, runs_root)
    with pytest.raises(SimulatedCrash):
        puller.pull()
    (interrupted,) = store.list_runs()
    assert store.checkpoint(interrupted.run_id).count_items() == 4
    assert store.checkpoint(interrupted.run_id).get_frontier("scripted.next_index") == 4

    source.crash_before_index = None
    report = puller.pull(resume=interrupted.run_id)
    assert report.item_count == 10
    assert report.status is RunStatus.COMPLETE


def test_items_delivered_twice_after_a_crash_are_not_duplicated(
    store: StateStore, runs_root: Path
) -> None:
    source = make_scripted(6)
    source.ignores_frontier = True  # re-delivers everything on resume
    puller = Puller(source, store, runs_root)
    first = puller.pull(stages=[Stage.LIST])
    again = puller.pull(resume=first.run_id, stages=[Stage.LIST])
    assert again.item_count == 6
    assert RunSnapshot.open(runs_root / first.run_id).verify() == []


def test_an_item_seen_in_two_folders_keeps_both_folder_paths(
    store: StateStore, runs_root: Path
) -> None:
    source = make_scripted(2)
    first = source.items[0]
    second_sighting = ListedItem(
        first.source_id, first.version, first.payload, {"folder_paths": ["Demo Library/Other"]}
    )
    source.items.append(second_sighting)
    report = Puller(source, store, runs_root).pull()

    assert report.item_count == 2
    stored = RunSnapshot.open(runs_root / report.run_id).get_item(first.source_id)
    assert stored.context["folder_paths"] == ["Demo Library/Woven", "Demo Library/Other"]


def test_merge_context_unions_lists_and_keeps_the_first_scalar() -> None:
    merged = merge_context(
        {"folder_paths": ["a", "b"], "depth": 1, "only_old": True},
        {"folder_paths": ["b", "c"], "depth": 2, "only_new": True},
    )
    assert merged == {
        "folder_paths": ["a", "b", "c"],
        "depth": 1,
        "only_old": True,
        "only_new": True,
    }


def test_effective_version_prefers_the_sources_own_token() -> None:
    with_version = ListedItem("a", "v7", {"x": 1})
    without = ListedItem("a", None, {"x": 1})
    assert effective_version(with_version) == "v7"
    assert effective_version(without).startswith("sha256:")
    assert effective_version(without) == effective_version(ListedItem("b", None, {"x": 1}))
    assert effective_version(without) != effective_version(ListedItem("a", None, {"x": 2}))


def test_duplicate_asset_roles_fail_the_item_not_the_run(
    store: StateStore, runs_root: Path
) -> None:
    class DuplicateRoles(ScriptedSource):
        def asset_refs(self, item: ListedItem, detail: dict[str, object] | None) -> list[AssetRef]:
            ref = AssetRef(item.source_id, "thumb", "https://assets.example.invalid/x")
            return [ref, ref] if item.source_id == sample_items.item_id(1) else [ref]

    base = make_scripted(3)
    source = DuplicateRoles(base.items, base.details, base.assets)
    report = Puller(source, store, runs_root).pull()
    (failure,) = report.failures
    assert failure.source_id == sample_items.item_id(1)
    assert "not unique" in failure.error
