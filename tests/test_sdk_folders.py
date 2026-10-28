"""The SDK's folder walker: its two resume granularities, and the parts the connectors configure.

The walk rules themselves (breadth-first order, fuses, cycles, the stop rule) are pinned by
each connector's walker tests, which run the same class with that connector's settings. What only
the SDK can show is the difference between the two ``resume`` settings.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from catalog_sync.errors import FuseTripped
from catalog_sync.models import JsonObject
from catalog_sync.sdk import FolderWalker, RowPage, Sighting, WalkSettings, folder_label
from catalog_sync.sdk.folders import Resume
from catalog_sync.state import Checkpoint, StateStore
from tests.doubles import SimulatedCrash

KEY = "test.walk"


def is_folder(row: Mapping[str, object]) -> bool:
    return row.get("kind") == "dir"


def item(n: int) -> JsonObject:
    return {"id": f"i{n}", "kind": "file", "name": f"item {n}"}


def directory(n: int, name: str) -> JsonObject:
    return {"id": f"d{n}", "kind": "dir", "name": name}


@dataclass
class Tree:
    """Serves a folder tree two rows to the page, and can die on the Nth request."""

    folders: dict[str, list[JsonObject]]
    totals: dict[str, int] = field(default_factory=dict)  # a total that differs from the truth
    crash_on_call: int | None = None
    calls: list[tuple[str, int]] = field(default_factory=list)

    def __call__(self, folder_id: str, page: int) -> RowPage:
        self.calls.append((folder_id, page))
        if self.crash_on_call == len(self.calls):
            raise SimulatedCrash
        rows = self.folders.get(folder_id, [])
        return RowPage(
            self.totals.get(folder_id, len(rows)), tuple(rows[(page - 1) * 2 : page * 2])
        )


# One big folder of five items (three pages) after a small one, so a kill can land mid-folder.
TREE = {
    "0": [directory(1, "big"), item(0)],
    "d1": [item(1), item(2), item(3), item(4), item(5)],
}


# The root holds exactly one page (two rows, page size two), so it costs a second, empty request:
# calls are (0,1) (0,2) (d1,1) (d1,2), and the fifth, (d1,3), is the one the kill lands on.
KILL_ON_CALL = 5


def walker(
    tree: Tree,
    resume: str | None = None,
    key: str = KEY,
    *,
    max_depth: int = 6,
    max_pages: int = 50,
) -> FolderWalker:
    """`resume=None` leaves the setting at its default."""
    options: dict[str, Resume] = {} if resume is None else {"resume": resume}  # type: ignore[dict-item]
    settings = WalkSettings("0", max_depth, max_pages, 2)
    return FolderWalker(tree, settings, frontier_key=key, is_folder=is_folder, **options)


@pytest.fixture
def checkpoint(store: StateStore) -> Checkpoint:
    store.create_run("run-1", "test.source")
    return store.checkpoint("run-1")


def kill_in_the_big_folder(checkpoint: Checkpoint, resume: str) -> Tree:
    """Die on the third page of folder d1; return what the resumed walk then asks for."""
    crashing = Tree(TREE, crash_on_call=KILL_ON_CALL)
    with pytest.raises(SimulatedCrash):
        list(walker(crashing, resume).walk(checkpoint))
    resumed = Tree(TREE)
    list(walker(resumed, resume).walk(checkpoint))
    return resumed


def test_page_resume_repeats_only_the_page_that_was_in_flight(checkpoint: Checkpoint) -> None:
    resumed = kill_in_the_big_folder(checkpoint, "page")
    assert resumed.calls == [("d1", 3)]


def test_folder_resume_repeats_the_whole_folder_it_died_in(checkpoint: Checkpoint) -> None:
    resumed = kill_in_the_big_folder(checkpoint, "folder")
    assert resumed.calls == [("d1", 1), ("d1", 2), ("d1", 3)]


@pytest.mark.parametrize("resume", ["page", "folder"])
def test_either_setting_delivers_every_item_across_a_kill(
    checkpoint: Checkpoint, resume: str
) -> None:
    delivered: list[Sighting] = []
    with pytest.raises(SimulatedCrash):
        for sighting in walker(Tree(TREE, crash_on_call=KILL_ON_CALL), resume).walk(checkpoint):
            delivered.append(sighting)
    delivered += walker(Tree(TREE), resume).walk(checkpoint)
    assert {s.source_id for s in delivered} == {f"i{n}" for n in range(6)}


def test_the_default_is_the_stronger_page_setting(checkpoint: Checkpoint) -> None:
    with pytest.raises(SimulatedCrash):
        list(walker(Tree(TREE, crash_on_call=KILL_ON_CALL)).walk(checkpoint))
    resumed = Tree(TREE)
    list(walker(resumed).walk(checkpoint))
    assert resumed.calls == [("d1", 3)]


def test_two_walks_under_different_keys_do_not_share_a_position(checkpoint: Checkpoint) -> None:
    list(walker(Tree(TREE), key="a.walk").walk(checkpoint))
    other = Tree(TREE)
    list(walker(other, key="b.walk").walk(checkpoint))
    assert other.calls[0] == ("0", 1), "the second key starts from the root"


def test_what_counts_as_a_folder_is_the_connectors_decision(checkpoint: Checkpoint) -> None:
    """The same rows read two ways: by `kind`, or never (a flat list is just a root folder)."""
    tree = Tree({"0": [directory(1, "x"), item(1)], "d1": [item(2)]})
    by_kind = list(walker(tree, "page").walk(checkpoint))
    assert {s.source_id for s in by_kind} == {"i1", "i2"}


def test_a_sighting_becomes_the_source_contracts_item_with_its_folder() -> None:
    inside = Sighting("i1", item(1), "a/b").listed_item("v1")
    assert (inside.source_id, inside.version, inside.context) == (
        "i1",
        "v1",
        {"folder_paths": ["a/b"]},
    )
    at_root = Sighting("i1", item(1), "").listed_item(None)
    assert at_root.version is None and at_root.context == {"folder_paths": []}


def test_a_folder_label_shows_the_name_and_the_id() -> None:
    assert folder_label({"id": "d1", "name": "Big"}) == "Big [d1]"
    assert folder_label({"id": "d2"}) == "(unnamed) [d2]"
    assert folder_label({"id": "d3", "name": ""}) == "(unnamed) [d3]"


# --- a total that is too low ------------------------------------------------------------------


def test_a_folder_whose_total_is_too_low_still_delivers_every_item(
    checkpoint: Checkpoint, caplog: pytest.LogCaptureFixture
) -> None:
    """The total of this folder says 1 while it holds 5 items; stopping at it lost four."""
    tree = Tree({"0": [item(n) for n in range(5)]}, totals={"0": 1})
    with caplog.at_level(logging.WARNING):
        sightings = list(walker(tree).walk(checkpoint))
    assert [s.source_id for s in sightings] == [f"i{n}" for n in range(5)]
    assert tree.calls == [("0", 1), ("0", 2), ("0", 3)]
    assert "folder 0: the service reports 1 rows but 5 came" in caplog.text


# --- a link back up the tree is not a deeper folder -------------------------------------------


def test_a_link_back_to_the_root_at_max_depth_is_skipped_not_a_fuse(
    checkpoint: Checkpoint,
) -> None:
    """Root -> d1 sits at depth 1 = max_depth; d1 lists the root again. Looked at as a folder at
    depth 2 it tripped "deeper than max_depth"; it is a folder the walk has already visited."""
    tree = Tree({"0": [directory(1, "a"), item(0)], "d1": [{"id": "0", "kind": "dir"}, item(1)]})
    sightings = list(walker(tree, max_depth=1).walk(checkpoint))
    assert {s.source_id for s in sightings} == {"i0", "i1"}


def test_a_link_to_a_folder_still_waiting_in_the_queue_is_skipped_too(
    checkpoint: Checkpoint,
) -> None:
    """d1 and d2 are both depth 1; d1 lists d2, which would be depth 2 if it were new."""
    tree = Tree(
        {
            "0": [directory(1, "a"), directory(2, "b")],
            "d1": [directory(2, "b"), item(1)],
            "d2": [item(2)],
        }
    )
    sightings = list(walker(tree, max_depth=1).walk(checkpoint))
    assert {s.source_id for s in sightings} == {"i1", "i2"}


def test_a_new_folder_deeper_than_max_depth_is_still_a_fuse(checkpoint: Checkpoint) -> None:
    tree = Tree({"0": [directory(1, "a")], "d1": [directory(2, "b")]})
    with pytest.raises(FuseTripped, match="deeper than max_depth=1"):
        list(walker(tree, max_depth=1).walk(checkpoint))


# --- max_pages counts the pages this process fetched ------------------------------------------


def test_a_resumed_walk_gets_a_fresh_page_allowance(checkpoint: Checkpoint) -> None:
    """Seven items are four pages. With max_pages=2 the first process stops after two; the second,
    with the same limit, fetches the other two. Counted from the start of the folder, the second
    would have tripped before its first request."""
    tree_rows = {"0": [item(n) for n in range(7)]}
    first = Tree(tree_rows)
    delivered: list[Sighting] = []
    with pytest.raises(FuseTripped, match="max_pages"):
        for sighting in walker(first, max_pages=2).walk(checkpoint):
            delivered.append(sighting)
    assert first.calls == [("0", 1), ("0", 2)]

    second = Tree(tree_rows)
    delivered += walker(second, max_pages=2).walk(checkpoint)
    assert second.calls == [("0", 3), ("0", 4)]
    assert {s.source_id for s in delivered} == {f"i{n}" for n in range(7)}
