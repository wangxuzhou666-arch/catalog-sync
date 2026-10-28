"""A source that serves a stored run snapshot instead of calling a service.

It is the second real implementation of the Source protocol (the first one talks to a live
API): it powers the credential-free README demo, the CI end-to-end run, and every test that
needs a pull without a network.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from catalog_sync.errors import ConfigError, ItemFailed
from catalog_sync.models import JsonObject
from catalog_sync.snapshot import RunSnapshot
from catalog_sync.sources.base import AssetRef, InspectReport, ListedItem
from catalog_sync.state import Checkpoint

_FRONTIER_KEY = "replay.next_index"


class ReplaySource:
    def __init__(self, snapshot: RunSnapshot) -> None:
        self._snapshot = snapshot
        # Records must say where they originally came from, not that they were replayed.
        self.name = snapshot.source

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> ReplaySource:
        unknown = set(options) - {"path"}
        if unknown:
            raise ConfigError(f"replay source: unknown option(s) {sorted(unknown)}")
        raw_path = options.get("path")
        if not isinstance(raw_path, str):
            raise ConfigError(
                "replay source needs a string option 'path' (a run snapshot directory)"
            )
        return cls(RunSnapshot.open(base_dir / Path(raw_path).expanduser()))

    def enumerate(self, state: Checkpoint) -> Iterator[ListedItem]:
        stored = state.get_frontier(_FRONTIER_KEY)
        if stored is None:
            start = 0
        elif isinstance(stored, int) and not isinstance(stored, bool):
            start = stored
        else:
            raise ConfigError(f"corrupt replay frontier in run {state.run_id}: {stored!r}")
        source_ids = self._snapshot.item_ids()
        for index in range(start, len(source_ids)):
            yield self._snapshot.get_item(source_ids[index])
            # Only after the consumer asks for the next item has it finished this one.
            state.set_frontier(_FRONTIER_KEY, index + 1)

    def fetch_detail(self, item: ListedItem) -> JsonObject | None:
        return self._snapshot.get_detail(item.source_id)

    def asset_refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        return self._snapshot.get_asset_refs(item.source_id) or []

    def fetch_asset(self, ref: AssetRef) -> bytes:
        if not self._snapshot.has_asset(ref):
            raise ItemFailed(ref.source_id, f"asset {ref.role!r} is not in the replayed snapshot")
        return self._snapshot.get_asset(ref)

    def inspect(self, folder: str | None) -> InspectReport:
        """Count the snapshot's items; folders are the first path segments recorded on them."""
        items = [self._snapshot.get_item(source_id) for source_id in self._snapshot.item_ids()]
        prefix = f"{folder}/" if folder else ""
        paths_of = [_folder_paths(item.context) for item in items]
        if folder is None:
            in_scope = paths_of
        else:
            in_scope = [p for p in paths_of if any(x == folder or x.startswith(prefix) for x in p)]
        labels = {
            path[len(prefix) :].split("/")[0]
            for paths in in_scope
            for path in paths
            if path.startswith(prefix) and path != folder
        }
        return InspectReport(
            total=len(in_scope),
            folders=tuple(sorted(labels)),
            notes=(f"replaying the snapshot of run {self._snapshot.run_id}",),
        )


def _folder_paths(context: JsonObject) -> list[str]:
    paths = context.get("folder_paths")
    return [path for path in paths if isinstance(path, str)] if isinstance(paths, list) else []
