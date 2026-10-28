"""Test doubles for the Source protocol: an in-memory source and a call-recording wrapper."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from catalog_sync.errors import ItemFailed
from catalog_sync.models import JsonObject
from catalog_sync.snapshot import RunSnapshot
from catalog_sync.sources.base import AssetRef, ListedItem, Source
from catalog_sync.state import Checkpoint
from catalog_sync.testing.fake_transport import SimulatedCrash
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.fixtures import mock_snapshot, sample_items

_FRONTIER_KEY = "scripted.next_index"


@dataclass
class ScriptedSource:
    """An in-memory Source whose listing, details and assets the test controls.

    Like a real source it keeps its enumeration frontier in the checkpoint and advances it only
    after the consumer asks for the next item.
    """

    items: list[ListedItem]
    details: dict[str, JsonObject | None]
    assets: dict[tuple[str, str], bytes]
    name: str = sample_items.SOURCE_NAME
    ignores_frontier: bool = False  # simulates a source that re-delivers everything on resume
    crash_before_index: int | None = None  # raise SimulatedCrash instead of yielding this index
    asset_roles: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def enumerate(self, state: Checkpoint) -> Iterator[ListedItem]:
        stored = state.get_frontier(_FRONTIER_KEY)
        start = 0 if self.ignores_frontier or stored is None else int(str(stored))
        for index in range(start, len(self.items)):
            if index == self.crash_before_index:
                raise SimulatedCrash
            yield self.items[index]
            state.set_frontier(_FRONTIER_KEY, index + 1)

    def fetch_detail(self, item: ListedItem) -> JsonObject | None:
        return self.details[item.source_id]

    def asset_refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        roles = self.asset_roles.get(item.source_id)
        if roles is None:
            roles = tuple(role for (sid, role) in self.assets if sid == item.source_id)
        return [
            AssetRef(item.source_id, role, f"{sample_items.ASSET_HOST}/{role}") for role in roles
        ]

    def fetch_asset(self, ref: AssetRef) -> bytes:
        try:
            return self.assets[(ref.source_id, ref.role)]
        except KeyError:
            raise ItemFailed(ref.source_id, f"no asset {ref.role!r}") from None


def make_scripted(count: int) -> ScriptedSource:
    """`count` items built from the synthetic builders, with details and assets for each."""
    items, details, assets = [], {}, {}
    for index in range(count):
        row = sample_items.list_row(index)
        context = {"folder_paths": sample_items.folder_paths(index)}
        source_id = sample_items.item_id(index)
        items.append(ListedItem(source_id, str(row["updated_at"]), row, context))
        details[source_id] = sample_items.detail_record(index)
        for role in sample_items.asset_roles(index):
            assets[(source_id, role)] = sample_items.png_bytes(index, role)
    return ScriptedSource(items, details, assets)


def make_scripted_mock(count: int) -> ScriptedSource:
    """The first `count` materials of the Mock-shaped example snapshot, as an in-memory source:
    the same listing rows, details and pictures a replay serves, with the test in control."""
    snapshot = RunSnapshot.open(MOCK_SNAPSHOT_DIR)
    source_ids = sorted(snapshot.item_ids())[:count]
    items = [snapshot.get_item(source_id) for source_id in source_ids]
    details = {source_id: snapshot.get_detail(source_id) for source_id in source_ids}
    assets = {
        (source_id, ref.role): snapshot.get_asset(ref)
        for source_id in source_ids
        for ref in snapshot.get_asset_refs(source_id) or []
    }
    return ScriptedSource(items, details, assets, name=mock_snapshot.SOURCE)


@dataclass
class Spy:
    """Wraps a Source, records every fetch, and lets a test inject a failure or a crash.

    A hook is called with the id (detail) or the ref (asset) before the real call is made; it
    raises to simulate a failure.
    """

    inner: Source
    before_detail: Callable[[str], None] | None = None
    before_asset: Callable[[AssetRef], None] | None = None
    detail_calls: list[str] = field(default_factory=list)
    asset_calls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.inner.name

    def enumerate(self, state: Checkpoint) -> Iterator[ListedItem]:
        return self.inner.enumerate(state)

    def fetch_detail(self, item: ListedItem) -> JsonObject | None:
        self.detail_calls.append(item.source_id)
        if self.before_detail is not None:
            self.before_detail(item.source_id)
        return self.inner.fetch_detail(item)

    def asset_refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        return self.inner.asset_refs(item, detail)

    def fetch_asset(self, ref: AssetRef) -> bytes:
        self.asset_calls.append((ref.source_id, ref.role))
        if self.before_asset is not None:
            self.before_asset(ref)
        return self.inner.fetch_asset(ref)
