"""Walk a folder tree breadth-first and report every item with the folder it was found in.

Many libraries list folders and items in one mixed listing; ``is_folder`` says which rows are the
folders to descend into. The walk is resumable: the position (the queue of folders still to visit,
the folder in progress and its next page, the folders already visited) is written to the run's
checkpoint under ``frontier_key``.

Crash behaviour follows the Source contract: the position advances only after the consumer has come
back for the next item, so a kill loses at most the page that was in flight, and that page is
fetched and reported again (the puller keeps the first sighting of an item and unions the folder
paths of later ones).

``resume`` sets how fine the saved position is. ``"page"`` (the default) saves after every page of
a folder, so a kill in the middle of a large folder repeats one page. ``"folder"`` saves only when
a folder is finished, which repeats the whole folder in that case; it is the simpler thing to
reason about, and enough when folders are small.

Fuses, because a tree deeper or longer than expected is a reason to stop and look, not to silently
walk less or more than asked: ``max_depth`` (a folder deeper than this trips it) and ``max_pages``.
Both raise FuseTripped *before* the page that revealed the problem is checkpointed, so a
``--resume`` finds the folder again.

``max_pages`` counts the pages this process fetches for one folder, as it does in the flat walkers
of `paging`: the count starts at zero in every process and for every folder, so a resumed run gets
a fresh allowance for the folder it was in (the pages fetched before the kill are not counted).
A folder ends on an empty page or a short one that reaches the reported total; `PageCursor` gives
the rule, and ``WalkSettings.page_size`` is the full-page size it needs.

A folder the walk already knows (visited, or waiting in the queue) is skipped before ``max_depth``
is looked at: a link back up the tree is not a deeper folder.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

from catalog_sync.errors import ConfigError, FuseTripped, SourceShapeError
from catalog_sync.models import JsonObject
from catalog_sync.sdk.paging import PageCursor, RowPage
from catalog_sync.sources.base import ListedItem
from catalog_sync.state import Checkpoint

logger = logging.getLogger(__name__)

_FRONTIER_FORMAT = 1

Resume = Literal["page", "folder"]
PageFetcher = Callable[[str, int], RowPage]  # (folder id, page number) -> that page


@dataclass(frozen=True)
class WalkSettings:
    """``page_size`` is the rows per page the fetcher asks for, so the walk can tell a full page
    (there may be more) from a short one (the folder is done)."""

    root_folder_id: str
    max_depth: int
    max_pages: int
    page_size: int


@dataclass(frozen=True)
class Sighting:
    """An item found in a folder. ``folder_path`` is "" for the walk's root folder."""

    source_id: str
    row: JsonObject
    folder_path: str

    def listed_item(self, version: str | None) -> ListedItem:
        """The Source-contract item: the row as received, and the folder it was found in."""
        context: JsonObject = {"folder_paths": [self.folder_path] if self.folder_path else []}
        return ListedItem(self.source_id, version, self.row, context)


@dataclass
class _Folder:
    folder_id: str
    path: str
    depth: int


@dataclass
class _Cursor:
    folder: _Folder
    pages: PageCursor


@dataclass
class _Frontier:
    queue: list[_Folder] = field(default_factory=list)
    current: _Cursor | None = None
    visited: set[str] = field(default_factory=set)


class FolderWalker:
    def __init__(
        self,
        fetch_page: PageFetcher,
        settings: WalkSettings,
        *,
        frontier_key: str,
        is_folder: Callable[[Mapping[str, object]], bool],
        resume: Resume = "page",
    ) -> None:
        self._fetch_page = fetch_page
        self._settings = settings
        self._frontier_key = frontier_key
        self._is_folder = is_folder
        self._resume = resume

    def walk(self, state: Checkpoint) -> Iterator[Sighting]:
        frontier = self._load(state)
        delivered: set[tuple[str, str]] = set()  # (item, folder path): the same row again is noise
        while True:
            cursor = frontier.current or self._start_next_folder(frontier)
            if cursor is None:
                return
            self._save(state, frontier)
            yield from self._walk_folder(state, frontier, cursor, delivered)
            frontier.current = None
            self._save(state, frontier)

    # --- one folder -------------------------------------------------------------------------

    def _walk_folder(
        self,
        state: Checkpoint,
        frontier: _Frontier,
        cursor: _Cursor,
        delivered: set[tuple[str, str]],
    ) -> Iterator[Sighting]:
        folder, pages = cursor.folder, cursor.pages
        fetched = 0  # pages of this folder fetched by this process (see the module docstring)
        while True:
            if fetched >= self._settings.max_pages:
                raise FuseTripped(
                    f"folder {folder.folder_id!r} has more than {self._settings.max_pages} pages "
                    "in this run; raise max_pages if it really is that large, then --resume"
                )
            page = self._fetch_page(folder.folder_id, pages.next_page)
            fetched += 1
            known = {queued.folder_id for queued in frontier.queue} | frontier.visited
            subfolders: list[_Folder] = []
            for row in page.rows:
                if self._is_folder(row):
                    self._note_subfolder(folder, row, known, subfolders)
                    continue
                sighting = Sighting(_row_id(row), row, folder.path)
                if (sighting.source_id, sighting.folder_path) not in delivered:
                    delivered.add((sighting.source_id, sighting.folder_path))
                    yield sighting
            # Control is back for every item of this page: now the position may move.
            pages.advance(page)
            frontier.queue.extend(subfolders)
            if pages.is_last(page):
                pages.warn_if_total_off(f"folder {folder.folder_id}", page)
                return
            if self._resume == "page":
                self._save(state, frontier)

    def _note_subfolder(
        self, parent: _Folder, row: JsonObject, known: set[str], found: list[_Folder]
    ) -> None:
        """Add the folder in ``row`` to ``found`` unless the walk already knows it. Known folders
        are checked first: a back-link to the root at ``max_depth`` is not a deeper folder."""
        folder_id = _row_id(row)
        if folder_id in known:
            logger.debug("folder %s is already known; not queued again", folder_id)
            return
        depth = parent.depth + 1
        if depth > self._settings.max_depth:
            raise FuseTripped(
                f"the folder tree is deeper than max_depth={self._settings.max_depth} below "
                f"{parent.path or 'the root'!r}. Raise max_depth if it really is that deep, "
                "then --resume"
            )
        known.add(folder_id)
        label = _label(row, folder_id)
        found.append(_Folder(folder_id, f"{parent.path}/{label}" if parent.path else label, depth))

    def _start_next_folder(self, frontier: _Frontier) -> _Cursor | None:
        if not frontier.queue:
            return None
        folder = frontier.queue.pop(0)
        frontier.visited.add(folder.folder_id)
        frontier.current = _Cursor(folder, PageCursor(self._settings.page_size))
        return frontier.current

    # --- checkpoint -------------------------------------------------------------------------

    def _load(self, state: Checkpoint) -> _Frontier:
        stored = state.get_frontier(self._frontier_key)
        if stored is None:
            return _Frontier(queue=[_Folder(self._settings.root_folder_id, "", 0)])
        try:
            return _decode(stored, self._settings.page_size)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(
                f"the folder-walk position of run {state.run_id} is unreadable "
                f"({type(exc).__name__}); start a new run instead of resuming it"
            ) from exc

    def _save(self, state: Checkpoint, frontier: _Frontier) -> None:
        current = frontier.current
        state.set_frontier(
            self._frontier_key,
            {
                "format": _FRONTIER_FORMAT,
                "queue": [_encode_folder(folder) for folder in frontier.queue],
                "current": None
                if current is None
                else {
                    **_encode_folder(current.folder),
                    "next_page": current.pages.next_page,
                    "rows_seen": current.pages.rows_seen,
                },
                "visited": sorted(frontier.visited),
            },
        )


def folder_label(row: Mapping[str, object]) -> str:
    """``name [id]``, for the folder list `inspect` prints."""
    name = row.get("name")
    return f"{name if isinstance(name, str) and name else '(unnamed)'} [{row.get('id')}]"


def _label(row: Mapping[str, object], folder_id: str) -> str:
    name = row.get("name")
    return name if isinstance(name, str) and name else folder_id


def _row_id(row: Mapping[str, object]) -> str:
    value = row.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)) or value == "":
        raise SourceShapeError("a listing row has no usable 'id'")
    return str(value)


def _encode_folder(folder: _Folder) -> JsonObject:
    return {"id": folder.folder_id, "path": folder.path, "depth": folder.depth}


def _decode(stored: object, page_size: int) -> _Frontier:
    if not isinstance(stored, dict) or stored.get("format") != _FRONTIER_FORMAT:
        raise ValueError("unknown frontier format")
    current = stored["current"]
    return _Frontier(
        queue=[_decode_folder(entry) for entry in stored["queue"]],
        current=None
        if current is None
        else _Cursor(
            _decode_folder(current),
            PageCursor(page_size, int(current["next_page"]), int(current["rows_seen"])),
        ),
        visited={str(folder_id) for folder_id in stored["visited"]},
    )


def _decode_folder(entry: object) -> _Folder:
    if not isinstance(entry, dict):
        raise TypeError("folder entry is not an object")
    return _Folder(str(entry["id"]), str(entry["path"]), int(entry["depth"]))
