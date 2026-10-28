"""What a source plugin has to provide, and the data it hands to the rest of the pipeline."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from catalog_sync.models import JsonObject
from catalog_sync.state import Checkpoint


@dataclass(frozen=True)
class ListedItem:
    """One row of a listing, exactly as the service returned it.

    ``version`` is whatever the service offers for change detection (an update timestamp, an
    etag). When it is None the puller falls back to a hash of ``payload``. ``context`` carries
    what the walk knew about the row that the row itself does not say, e.g. its folder paths.
    """

    source_id: str
    version: str | None
    payload: JsonObject
    context: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class AssetRef:
    source_id: str
    role: str  # unique per item; becomes part of the stored file name
    url: str


@dataclass(frozen=True)
class StoredAsset:
    """An asset already saved in a run snapshot.

    `width` and `height` are read from the picture's header when the bundle is assembled; both are
    None for an asset that is not a readable picture, in which case `is_unreadable` says so.
    """

    ref: AssetRef
    name: str
    sha256: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    is_unreadable: bool = False


@dataclass(frozen=True)
class RawBundle:
    """Everything captured for one item. List and detail stay separate on purpose: neither is a
    superset of the other, and merging them blindly lets an empty detail value wipe a list value.
    """

    item: ListedItem
    detail: JsonObject | None
    assets: tuple[StoredAsset, ...]


class Source(Protocol):
    """A place records can be pulled from. Implementations: ReplaySource, the Mock sources."""

    name: str

    def enumerate(self, state: Checkpoint) -> Iterator[ListedItem]:
        """Yield every listed item, resuming from whatever frontier `state` holds.

        Advance the stored frontier only after control returns from ``yield``: that way a crash
        while the consumer is still handling an item makes the next run deliver it again,
        instead of silently skipping it. Yielding an item twice is allowed; the puller keeps the
        first and merges list-valued context entries (an item that lives in two folders).
        """
        ...

    def fetch_detail(self, item: ListedItem) -> JsonObject | None:
        """Fetch the detail record, or None when the source has no detail for this item.

        Raise ItemFailed for a problem confined to this item; anything else aborts the run.
        """
        ...

    def asset_refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        """Name the binary assets to download. Pure: no network."""
        ...

    def fetch_asset(self, ref: AssetRef) -> bytes: ...


@dataclass(frozen=True)
class InspectReport:
    """What `catalog-sync inspect` shows: a cheap look at a source, without pulling it."""

    total: int | None  # what the source says it holds in the inspected scope; None if unknown
    folders: tuple[str, ...]  # labels of the sub-folders visible in that scope
    notes: tuple[str, ...]  # anything else worth seeing: field names, caveats


@runtime_checkable
class Inspectable(Protocol):
    """A source that can be looked at without pulling it. Implementations: Replay, Mock."""

    def inspect(self, folder: str | None) -> InspectReport:
        """Look at `folder` (the source's own id for it; None means its default root)."""
        ...
