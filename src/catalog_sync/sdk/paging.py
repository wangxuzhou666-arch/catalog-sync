"""The three pagination styles web APIs use, each with the same two halves.

* a ``*_page`` function that checks one decoded block of a response and returns a typed page, and
* a ``walk_*_pages`` generator that follows the pages and keeps its place in the run's checkpoint.

=================  ===========================  ==========================================
style              page                         position kept in the checkpoint
=================  ===========================  ==========================================
page number        ``RowPage`` (rows + total)   ``{"next_page", "rows_seen"}``
next-page token    ``TokenPage``                the token of the page about to be read
offset and limit   ``OffsetPage``               the offset of the page about to be read
=================  ===========================  ==========================================

The position moves only after the consumer has come back for the next page, so a run killed in the
middle of a page reads that page again instead of skipping the rest of it (the Source contract
allows an item to be delivered twice; the puller keeps the first sighting).

``PageCursor`` is the page-number stop rule on its own, for callers that keep their own place
(the folder walk keeps one per folder).

``max_pages`` is a fuse on the pages *this process* fetches for one listing, the same in all three
walkers: a walk that is resumed starts a fresh count, so a run stopped by the fuse carries on for
another ``max_pages`` pages once resumed (raising the limit is only needed when one process has to
go further than that). The pages fetched by earlier processes are not counted, they are what the
saved position already covers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from catalog_sync.errors import ConfigError, FuseTripped, SourceShapeError
from catalog_sync.models import JsonObject
from catalog_sync.state import Checkpoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RowPage:
    """One page of a listing that reports a total. ``total`` counts every row of the listing, not
    just this page, folders and items alike when they share one."""

    total: int
    rows: tuple[JsonObject, ...]


@dataclass(frozen=True)
class TokenPage:
    rows: tuple[JsonObject, ...]
    next_token: str | None  # None on the last page


@dataclass(frozen=True)
class OffsetPage:
    rows: tuple[JsonObject, ...]
    total: int | None  # None when the service does not say


# --- reading a response block ------------------------------------------------------------------


def rows_page(
    block: Mapping[str, object], *, where: str, total_key: str = "total", rows_key: str = "rows"
) -> RowPage:
    """``where`` names the block in errors (``data``, ``data.list``)."""
    total = block.get(total_key)
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SourceShapeError(f"{where}.{total_key} is not a non-negative integer (got {total!r})")
    return RowPage(total, _rows(block, where, rows_key))


def token_page(
    block: Mapping[str, object],
    *,
    where: str,
    rows_key: str = "items",
    next_key: str = "next_cursor",
) -> TokenPage:
    rows = _rows(block, where, rows_key)
    token = block.get(next_key)
    if token is not None and (not isinstance(token, str) or not token):
        raise SourceShapeError(f"{where}.{next_key} is neither null nor a cursor (got {token!r})")
    return TokenPage(rows, token)


def offset_page(
    block: Mapping[str, object], *, where: str, rows_key: str = "items", total_key: str = "total"
) -> OffsetPage:
    rows = _rows(block, where, rows_key)
    total = block.get(total_key)
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        raise SourceShapeError(f"{where}.{total_key} is not a non-negative integer (got {total!r})")
    return OffsetPage(rows, total)


def _rows(block: Mapping[str, object], where: str, key: str) -> tuple[JsonObject, ...]:
    rows = block.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SourceShapeError(f"{where}.{key} is not a list of objects")
    return tuple(rows)


# --- page number ---------------------------------------------------------------------------------


@dataclass
class PageCursor:
    """Where a page-number listing stands: the next page to ask for, and how many rows have come.

    ``page_size`` is how many rows a full page holds (the size the request asked for). A listing
    ends on an empty page, or on a *short* page (fewer rows than ``page_size``) once the rows seen
    have reached the reported total. A full page that reaches the total does not end it: a total
    that is too low (it counts items while the rows also hold folders, say) would otherwise drop
    every row after that page without a trace, so the next page is asked for and only an empty or
    short page settles it. The price is one extra request when the listing is an exact multiple of
    ``page_size``. A total that is too high (rows removed while paging) ends on the empty page
    instead of looping.
    """

    page_size: int
    next_page: int = 1
    rows_seen: int = 0

    def advance(self, page: RowPage) -> None:
        self.rows_seen += len(page.rows)
        self.next_page += 1

    def is_last(self, page: RowPage) -> bool:
        """Call after ``advance``."""
        if not page.rows:
            return True
        if self.rows_seen < page.total:
            return False
        return len(page.rows) < self.page_size

    def warn_if_total_off(self, what: str, page: RowPage) -> None:
        """Call once the listing has ended: the rows delivered should equal the reported total."""
        _warn_if_total_off(what, page.total, self.rows_seen)


def _warn_if_total_off(what: str, total: int, delivered: int) -> None:
    if delivered < total:
        logger.warning(
            "%s: the service reports %d rows but stopped after %d (an empty page); "
            "items may have been removed during the walk",
            what,
            total,
            delivered,
        )
    elif delivered > total:
        logger.warning(
            "%s: the service reports %d rows but %d came; its total is too low "
            "(every row that came was kept)",
            what,
            total,
            delivered,
        )


def walk_number_pages(
    fetch: Callable[[int], RowPage],
    state: Checkpoint,
    *,
    key: str,
    page_size: int,
    max_pages: int,
    what: str,
) -> Iterator[RowPage]:
    """Follow ``fetch(page_number)`` until the listing ends (see `PageCursor` for the stop rule).
    ``page_size`` is the rows per page the requests ask for and ``what`` names the listing.
    ``max_pages`` bounds the pages this call fetches, a resumed walk included."""
    cursor = _stored_cursor(state, key, what, page_size)
    for _ in range(max_pages):
        page = fetch(cursor.next_page)
        yield page
        cursor.advance(page)
        if cursor.is_last(page):
            cursor.warn_if_total_off(what, page)
            return
        state.set_frontier(key, {"next_page": cursor.next_page, "rows_seen": cursor.rows_seen})
    raise _too_many_pages(what, max_pages)


def _stored_cursor(state: Checkpoint, key: str, what: str, page_size: int) -> PageCursor:
    stored = state.get_frontier(key)
    if stored is None:
        return PageCursor(page_size)
    if not isinstance(stored, dict) or not all(
        isinstance(stored.get(name), int) for name in ("next_page", "rows_seen")
    ):
        raise ConfigError(_unreadable(state, what))
    return PageCursor(page_size, stored["next_page"], stored["rows_seen"])


# --- next-page token -----------------------------------------------------------------------------


def walk_token_pages(
    fetch: Callable[[str | None], TokenPage],
    state: Checkpoint,
    *,
    key: str,
    max_pages: int,
    what: str,
) -> Iterator[TokenPage]:
    """Follow the tokens: ``fetch(None)`` for the first page, then each page's ``next_token``.

    A token the service hands out twice would loop forever, so one that does not advance stops the
    walk with an error instead. ``max_pages`` bounds the pages this call fetches, a resumed walk
    included.
    """
    stored = state.get_frontier(key)
    if stored is not None and not isinstance(stored, str):
        raise ConfigError(_unreadable(state, what))
    token = stored
    seen_tokens: set[str] = set()
    for _ in range(max_pages):
        page = fetch(token)
        yield page
        if page.next_token is None:
            return
        if page.next_token in seen_tokens or page.next_token == token:
            raise SourceShapeError(
                "the service's cursor did not advance; stopping instead of looping"
            )
        seen_tokens.add(page.next_token)
        token = page.next_token
        state.set_frontier(key, token)
    raise _too_many_pages(what, max_pages)


# --- offset and limit ----------------------------------------------------------------------------


def walk_offset_pages(
    fetch: Callable[[int, int], OffsetPage],
    state: Checkpoint,
    *,
    key: str,
    limit: int,
    max_pages: int,
    what: str,
) -> Iterator[OffsetPage]:
    """Follow ``fetch(offset, limit)``. The next offset is the rows actually received, not the
    limit asked for: a service may cap the limit below what was requested, and stepping by the
    request would skip rows.

    An empty page ends the walk. So does a *short* page (fewer rows than ``limit``) that reaches a
    reported total; a full page that reaches the total does not, for the reason `PageCursor`
    gives (a total that is too low must not drop rows). The walk warns when it ends with a
    delivered count other than the reported total. ``max_pages`` bounds the pages this call
    fetches, a resumed walk included.
    """
    stored = state.get_frontier(key)
    if stored is not None and (isinstance(stored, bool) or not isinstance(stored, int)):
        raise ConfigError(_unreadable(state, what))
    offset = stored or 0
    for _ in range(max_pages):
        page = fetch(offset, limit)
        yield page
        offset += len(page.rows)
        reached_total = page.total is not None and offset >= page.total
        if not page.rows or (reached_total and len(page.rows) < limit):
            if page.total is not None:
                _warn_if_total_off(what, page.total, offset)
            return
        state.set_frontier(key, offset)
    raise _too_many_pages(what, max_pages)


def _unreadable(state: Checkpoint, what: str) -> str:
    return (
        f"the {what} position of run {state.run_id} is unreadable; "
        "start a new run instead of resuming it"
    )


def _too_many_pages(what: str, max_pages: int) -> FuseTripped:
    """The same fuse the folder walk has (exit code 3): the service is not misbehaving in shape,
    the listing is longer than the operator allowed, which `max_pages` and a resume settle."""
    return FuseTripped(
        f"more than {max_pages} pages of {what}; raise max_pages if that is right, then --resume"
    )
