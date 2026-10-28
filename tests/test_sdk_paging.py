"""The three pagination styles of the connector SDK, against fixtures that behave like services.

The page-number and token walkers are what the shipped connectors rely on; the offset walker is
what ``new-connector --pagination offset`` generates, so its cases are written here against a fake
listing that can misbehave the way real ones do (a capped limit, an ignored offset, no total).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from catalog_sync.errors import ConfigError, FuseTripped, SourceShapeError
from catalog_sync.models import JsonObject
from catalog_sync.sdk import (
    OffsetPage,
    PageCursor,
    RowPage,
    TokenPage,
    offset_page,
    rows_page,
    token_page,
    walk_number_pages,
    walk_offset_pages,
    walk_token_pages,
)
from catalog_sync.state import Checkpoint, StateStore
from tests.conftest import REPO_ROOT
from tests.doubles import SimulatedCrash

KEY = "test.position"


@pytest.fixture
def checkpoint(store: StateStore) -> Checkpoint:
    store.create_run("run-1", "test.source")
    return store.checkpoint("run-1")


def row(n: int) -> JsonObject:
    return {"id": f"r{n}"}


def ids(pages: object) -> list[str]:
    return [str(r["id"]) for page in pages for r in page.rows]  # type: ignore[attr-defined]


# --- reading a response block ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "complaint"),
    [
        ({"rows": []}, r"data\.total is not a non-negative integer \(got None\)"),
        ({"total": -1, "rows": []}, r"data\.total"),
        ({"total": True, "rows": []}, r"data\.total"),
        ({"total": "3", "rows": []}, r"data\.total"),
        ({"total": 1}, r"data\.rows is not a list of objects"),
        ({"total": 1, "rows": [1]}, r"data\.rows is not a list of objects"),
    ],
)
def test_a_row_page_of_the_wrong_shape_names_the_field(
    block: dict[str, object], complaint: str
) -> None:
    with pytest.raises(SourceShapeError, match=complaint):
        rows_page(block, where="data")


def test_a_row_page_reads_the_keys_it_is_told_to() -> None:
    page = rows_page(
        {"count": 2, "list": [row(1), row(2)]}, where="body", total_key="count", rows_key="list"
    )
    assert page == RowPage(2, (row(1), row(2)))


@pytest.mark.parametrize("token", ["", 5, ["x"]])
def test_a_token_that_is_neither_null_nor_text_is_refused(token: object) -> None:
    with pytest.raises(SourceShapeError, match="neither null nor a cursor"):
        token_page({"items": [], "next_cursor": token}, where="data")


def test_a_token_page_may_end_with_null_or_continue_with_text() -> None:
    assert token_page({"items": [row(1)], "next_cursor": None}, where="data").next_token is None
    assert token_page({"items": [], "next_cursor": "t2"}, where="data").next_token == "t2"


def test_an_offset_page_may_omit_the_total_but_not_garble_it() -> None:
    assert offset_page({"items": [row(1)]}, where="data") == OffsetPage((row(1),), None)
    assert offset_page({"items": [], "total": 0}, where="data").total == 0
    for bad in (-1, True, "9"):
        with pytest.raises(SourceShapeError, match=r"data\.total"):
            offset_page({"items": [], "total": bad}, where="data")


# --- the page-number stop rule --------------------------------------------------------------------


def test_the_cursor_stops_on_an_empty_page_or_a_short_page_at_the_total() -> None:
    cursor = PageCursor(page_size=2)
    full = RowPage(5, (row(1), row(2)))
    cursor.advance(full)
    assert (cursor.next_page, cursor.rows_seen, cursor.is_last(full)) == (2, 2, False)
    cursor.advance(full)
    assert not cursor.is_last(full), "4 of 5 rows seen"
    short = RowPage(5, (row(5),))
    cursor.advance(short)
    assert cursor.is_last(short), "a short page that reaches the total is the last"

    empty = PageCursor(page_size=2)
    nothing = RowPage(99, ())
    empty.advance(nothing)
    assert empty.is_last(nothing), "an empty page ends it whatever the total promises"


def test_a_full_page_that_reaches_the_total_is_not_taken_for_the_last() -> None:
    """The total may be too low (it can count items only while the rows hold folders too): ending
    on it dropped every row after the page, silently."""
    cursor = PageCursor(page_size=3)
    page = RowPage(1, (row(1), row(2), row(3)))
    cursor.advance(page)
    assert not cursor.is_last(page), "rows seen (3) passed the total (1) on a full page: ask again"

    exact = PageCursor(page_size=2)
    page = RowPage(2, (row(1), row(2)))
    exact.advance(page)
    assert not exact.is_last(page), "full and exactly at the total: only an empty page proves it"
    exact.advance(RowPage(2, ()))
    assert exact.is_last(RowPage(2, ()))


# --- page number ----------------------------------------------------------------------------------


@dataclass
class NumberedListing:
    rows: list[JsonObject]
    per_page: int = 2
    total: int | None = None
    crash_on_call: int | None = None
    calls: list[int] = field(default_factory=list)

    def __call__(self, page: int) -> RowPage:
        self.calls.append(page)
        if self.crash_on_call == len(self.calls):
            raise SimulatedCrash
        window = self.rows[(page - 1) * self.per_page : page * self.per_page]
        return RowPage(len(self.rows) if self.total is None else self.total, tuple(window))


def number_walk(
    listing: Callable[[int], RowPage],
    checkpoint: Checkpoint,
    *,
    page_size: int = 2,
    max_pages: int = 50,
) -> list[RowPage]:
    return list(
        walk_number_pages(
            listing, checkpoint, key=KEY, page_size=page_size, max_pages=max_pages, what="items"
        )
    )


def test_number_pages_are_followed_until_the_total_is_reached(checkpoint: Checkpoint) -> None:
    listing = NumberedListing([row(n) for n in range(5)])
    assert ids(number_walk(listing, checkpoint)) == ["r0", "r1", "r2", "r3", "r4"]
    assert listing.calls == [1, 2, 3], "no fourth request"


def test_an_empty_page_ends_the_walk_when_the_total_promises_more(
    checkpoint: Checkpoint, caplog: pytest.LogCaptureFixture
) -> None:
    listing = NumberedListing([row(n) for n in range(4)], total=10)
    with caplog.at_level(logging.WARNING):
        assert len(ids(number_walk(listing, checkpoint))) == 4
    assert listing.calls == [1, 2, 3]
    assert "items: the service reports 10 rows but stopped after 4" in caplog.text


def test_a_total_that_is_too_low_does_not_drop_rows(
    checkpoint: Checkpoint, caplog: pytest.LogCaptureFixture
) -> None:
    """The regression: total=1 on a listing of 5 rows stopped after page one and lost r2..r4."""
    listing = NumberedListing([row(n) for n in range(5)], total=1)
    with caplog.at_level(logging.WARNING):
        assert ids(number_walk(listing, checkpoint)) == [f"r{n}" for n in range(5)]
    assert listing.calls == [1, 2, 3], "full pages carry on past the total; the short one ends it"
    assert "items: the service reports 1 rows but 5 came; its total is too low" in caplog.text


def test_a_total_too_low_by_exactly_the_pages_read_still_carries_on(checkpoint: Checkpoint) -> None:
    """The sharper case: the rows seen land exactly on the total at the end of a full page."""
    listing = NumberedListing([row(n) for n in range(7)], total=4)
    assert ids(number_walk(listing, checkpoint)) == [f"r{n}" for n in range(7)]
    assert listing.calls == [1, 2, 3, 4]


def test_a_listing_that_fills_its_last_page_costs_one_empty_request(
    checkpoint: Checkpoint, caplog: pytest.LogCaptureFixture
) -> None:
    listing = NumberedListing([row(n) for n in range(4)])
    with caplog.at_level(logging.WARNING):
        assert len(ids(number_walk(listing, checkpoint))) == 4
    assert listing.calls == [1, 2, 3], "a full page at the total could be followed by more rows"
    assert caplog.text == "", "an honest total is not a warning"


def test_a_listing_that_fits_one_short_page_is_one_request(checkpoint: Checkpoint) -> None:
    listing = NumberedListing([row(n) for n in range(3)], per_page=50)
    assert len(ids(number_walk(listing, checkpoint, page_size=50))) == 3
    assert listing.calls == [1]


def test_more_pages_than_the_limit_is_an_error_not_a_silent_cut(checkpoint: Checkpoint) -> None:
    listing = NumberedListing([row(n) for n in range(20)], per_page=1)
    with pytest.raises(FuseTripped, match="more than 3 pages of items"):
        number_walk(listing, checkpoint, max_pages=3)
    assert listing.calls == [1, 2, 3]


def test_the_number_position_moves_only_after_the_consumer_is_done(checkpoint: Checkpoint) -> None:
    listing = NumberedListing([row(n) for n in range(6)])
    walk = walk_number_pages(listing, checkpoint, key=KEY, page_size=2, max_pages=9, what="items")
    next(walk)
    assert checkpoint.get_frontier(KEY) is None, "still inside page one"
    next(walk)
    assert checkpoint.get_frontier(KEY) == {"next_page": 2, "rows_seen": 2}


def test_a_killed_number_walk_resumes_at_the_page_it_was_on(checkpoint: Checkpoint) -> None:
    rows = [row(n) for n in range(7)]
    crashing = NumberedListing(rows, crash_on_call=3)
    delivered: list[RowPage] = []
    with pytest.raises(SimulatedCrash):
        for page in walk_number_pages(
            crashing, checkpoint, key=KEY, page_size=2, max_pages=9, what="items"
        ):
            delivered.append(page)
    resumed = NumberedListing(rows)
    delivered += number_walk(resumed, checkpoint)
    assert resumed.calls[0] == 3, "pages one and two are not read again"
    assert ids(delivered) == [f"r{n}" for n in range(7)]


@pytest.mark.parametrize(
    "garbage", ["text", 7, {"next_page": 1}, {"next_page": "1", "rows_seen": 0}]
)
def test_an_unreadable_number_position_asks_for_a_new_run(
    checkpoint: Checkpoint, garbage: object
) -> None:
    checkpoint.set_frontier(KEY, garbage)
    with pytest.raises(ConfigError, match=r"run-1.*unreadable"):
        number_walk(NumberedListing([]), checkpoint)


# --- next-page token ------------------------------------------------------------------------------


@dataclass
class TokenListing:
    """Pages of two rows; token ``tN`` is the page starting at row N."""

    rows: list[JsonObject]
    stuck_on: str = "-"  # the service hands this token out again instead of moving on
    calls: list[str | None] = field(default_factory=list)

    def __call__(self, token: str | None) -> TokenPage:
        self.calls.append(token)
        start = 0 if token is None else int(token.removeprefix("t"))
        window = tuple(self.rows[start : start + 2])
        more = start + 2 < len(self.rows)
        next_token = f"t{start + 2}" if more else None
        if token == self.stuck_on:
            next_token = token
        return TokenPage(window, next_token)


def token_walk(
    listing: Callable[[str | None], TokenPage], checkpoint: Checkpoint, **kw: int
) -> list[TokenPage]:
    return list(
        walk_token_pages(
            listing, checkpoint, key=KEY, max_pages=kw.get("max_pages", 50), what="items"
        )
    )


def test_token_pages_are_followed_to_a_null_token(checkpoint: Checkpoint) -> None:
    listing = TokenListing([row(n) for n in range(5)])
    assert ids(token_walk(listing, checkpoint)) == [f"r{n}" for n in range(5)]
    assert listing.calls == [None, "t2", "t4"]


def test_a_token_that_does_not_advance_stops_the_walk(checkpoint: Checkpoint) -> None:
    listing = TokenListing([row(n) for n in range(9)], stuck_on="t2")
    with pytest.raises(SourceShapeError, match="cursor did not advance"):
        token_walk(listing, checkpoint)
    assert listing.calls == [None, "t2"], "it read the page that repeated, and not another"


def test_a_token_seen_before_stops_the_walk_even_when_it_is_not_the_current_one(
    checkpoint: Checkpoint,
) -> None:
    sequence = {None: "a", "a": "b", "b": "a"}  # a cycle of length two

    def cyclic(token: str | None) -> TokenPage:
        return TokenPage((), sequence[token])

    with pytest.raises(SourceShapeError, match="did not advance"):
        token_walk(cyclic, checkpoint)


def test_a_token_walk_longer_than_max_pages_is_an_error(checkpoint: Checkpoint) -> None:
    with pytest.raises(FuseTripped, match="more than 2 pages of items"):
        token_walk(TokenListing([row(n) for n in range(20)]), checkpoint, max_pages=2)


def test_the_token_position_is_the_page_about_to_be_read(checkpoint: Checkpoint) -> None:
    listing = TokenListing([row(n) for n in range(6)])
    walk = walk_token_pages(listing, checkpoint, key=KEY, max_pages=9, what="items")
    next(walk)
    assert checkpoint.get_frontier(KEY) is None
    next(walk)
    assert checkpoint.get_frontier(KEY) == "t2"
    walk.close()
    resumed = TokenListing([row(n) for n in range(6)])
    assert ids(token_walk(resumed, checkpoint)) == ["r2", "r3", "r4", "r5"]
    assert resumed.calls[0] == "t2"


def test_an_unreadable_token_position_asks_for_a_new_run(checkpoint: Checkpoint) -> None:
    checkpoint.set_frontier(KEY, {"oops": 1})
    with pytest.raises(ConfigError, match=r"run-1.*unreadable"):
        token_walk(TokenListing([]), checkpoint)


# --- offset and limit -----------------------------------------------------------------------------


@dataclass
class OffsetListing:
    rows: list[JsonObject]
    reports_total: bool = True
    cap: int | None = None  # the service serves at most this many rows whatever the limit
    ignores_offset: bool = False
    reported_total: int | None = None  # what the service says instead of the true count
    crash_on_call: int | None = None
    calls: list[tuple[int, int]] = field(default_factory=list)

    def __call__(self, offset: int, limit: int) -> OffsetPage:
        self.calls.append((offset, limit))
        if self.crash_on_call == len(self.calls):
            raise SimulatedCrash
        size = limit if self.cap is None else min(limit, self.cap)
        start = 0 if self.ignores_offset else offset
        total = len(self.rows) if self.reported_total is None else self.reported_total
        total = total if self.reports_total else None
        return OffsetPage(tuple(self.rows[start : start + size]), total)


def offset_walk(
    listing: Callable[[int, int], OffsetPage],
    checkpoint: Checkpoint,
    *,
    limit: int = 3,
    max_pages: int = 50,
) -> list[OffsetPage]:
    return list(
        walk_offset_pages(
            listing, checkpoint, key=KEY, limit=limit, max_pages=max_pages, what="items"
        )
    )


def test_offsets_advance_by_the_rows_received_until_the_total(checkpoint: Checkpoint) -> None:
    listing = OffsetListing([row(n) for n in range(7)])
    assert ids(offset_walk(listing, checkpoint)) == [f"r{n}" for n in range(7)]
    assert listing.calls == [(0, 3), (3, 3), (6, 3)], "no request beyond the total"


def test_without_a_total_an_empty_page_ends_the_walk(checkpoint: Checkpoint) -> None:
    listing = OffsetListing([row(n) for n in range(6)], reports_total=False)
    assert len(ids(offset_walk(listing, checkpoint))) == 6
    assert listing.calls == [(0, 3), (3, 3), (6, 3)], "the empty page is how it knows"


def test_a_service_that_caps_the_limit_is_not_skipped_over(checkpoint: Checkpoint) -> None:
    """Stepping by the limit asked for (3) instead of the rows received (2) would lose a row on
    every page; a short page is not the last page."""
    listing = OffsetListing([row(n) for n in range(7)], cap=2)
    assert ids(offset_walk(listing, checkpoint)) == [f"r{n}" for n in range(7)]
    assert [offset for offset, _ in listing.calls] == [0, 2, 4, 6]


def test_a_total_that_is_too_low_does_not_drop_offset_rows(
    checkpoint: Checkpoint, caplog: pytest.LogCaptureFixture
) -> None:
    listing = OffsetListing([row(n) for n in range(8)])
    listing.reported_total = 2
    with caplog.at_level(logging.WARNING):
        assert ids(offset_walk(listing, checkpoint)) == [f"r{n}" for n in range(8)]
    assert listing.calls == [(0, 3), (3, 3), (6, 3)], "full pages go on; the short one ends it"
    assert "items: the service reports 2 rows but 8 came; its total is too low" in caplog.text


def test_a_full_offset_page_at_the_total_costs_one_empty_request(
    checkpoint: Checkpoint, caplog: pytest.LogCaptureFixture
) -> None:
    listing = OffsetListing([row(n) for n in range(6)])
    with caplog.at_level(logging.WARNING):
        assert len(ids(offset_walk(listing, checkpoint))) == 6
    assert listing.calls == [(0, 3), (3, 3), (6, 3)]
    assert caplog.text == ""


def test_an_empty_page_before_the_total_warns(
    checkpoint: Checkpoint, caplog: pytest.LogCaptureFixture
) -> None:
    listing = OffsetListing([row(n) for n in range(4)])
    listing.reported_total = 10
    with caplog.at_level(logging.WARNING):
        assert len(ids(offset_walk(listing, checkpoint))) == 4
    assert "items: the service reports 10 rows but stopped after 4" in caplog.text


def test_an_empty_listing_is_one_request(checkpoint: Checkpoint) -> None:
    listing = OffsetListing([])
    assert offset_walk(listing, checkpoint) == [OffsetPage((), 0)]
    assert listing.calls == [(0, 3)]


def test_a_service_that_ignores_the_offset_trips_max_pages_instead_of_looping(
    checkpoint: Checkpoint,
) -> None:
    listing = OffsetListing([row(n) for n in range(30)], reports_total=False, ignores_offset=True)
    with pytest.raises(FuseTripped, match="more than 4 pages of items"):
        offset_walk(listing, checkpoint, max_pages=4)
    assert len(listing.calls) == 4


def test_a_killed_offset_walk_resumes_at_the_offset_it_reached(checkpoint: Checkpoint) -> None:
    rows = [row(n) for n in range(9)]
    crashing = OffsetListing(rows, crash_on_call=3)
    delivered: list[OffsetPage] = []
    with pytest.raises(SimulatedCrash):
        for page in walk_offset_pages(
            crashing, checkpoint, key=KEY, limit=3, max_pages=9, what="items"
        ):
            delivered.append(page)
    assert checkpoint.get_frontier(KEY) == 3 + 3
    resumed = OffsetListing(rows)
    delivered += offset_walk(resumed, checkpoint)
    assert resumed.calls[0] == (6, 3), "the two finished pages are not read again"
    assert ids(delivered) == [f"r{n}" for n in range(9)]


@pytest.mark.parametrize("garbage", ["3", True, {"offset": 3}, 2.5])
def test_an_unreadable_offset_position_asks_for_a_new_run(
    checkpoint: Checkpoint, garbage: object
) -> None:
    checkpoint.set_frontier(KEY, garbage)
    with pytest.raises(ConfigError, match=r"run-1.*unreadable"):
        offset_walk(OffsetListing([]), checkpoint)


# --- the page limit is a fuse, like the folder walk's ---------------------------------------------


def test_a_flat_listing_that_hits_max_pages_is_a_fuse_with_exit_code_3_and_resumes(
    checkpoint: Checkpoint,
) -> None:
    """Before, only the folder walk raised FuseTripped (so the run ended `fused`, exit code 3,
    resumable) while the flat paginators raised SourceShapeError for the same situation: a
    listing longer than the operator allowed is not a response of the wrong shape. The saved
    position lets a larger limit carry on instead of starting over."""
    rows = [row(n) for n in range(7)]
    tripped = NumberedListing(rows, per_page=2)
    with pytest.raises(FuseTripped, match="--resume") as caught:
        number_walk(tripped, checkpoint, max_pages=2)
    assert caught.value.exit_code == 3
    assert tripped.calls == [1, 2]

    resumed = NumberedListing(rows, per_page=2)
    assert ids(number_walk(resumed, checkpoint, max_pages=9)) == ["r4", "r5", "r6"]
    assert resumed.calls == [3, 4], "it continues where the fuse stopped"


def test_the_token_walk_trips_the_same_fuse(checkpoint: Checkpoint) -> None:
    with pytest.raises(FuseTripped):
        token_walk(TokenListing([row(n) for n in range(9)]), checkpoint, max_pages=2)


def test_the_offset_walk_trips_the_same_fuse(checkpoint: Checkpoint) -> None:
    with pytest.raises(FuseTripped):
        offset_walk(OffsetListing([row(n) for n in range(30)]), checkpoint, max_pages=2)


# --- max_pages counts the pages this process fetched, in every walker -------------------------


def test_a_resumed_number_walk_gets_a_fresh_page_allowance(checkpoint: Checkpoint) -> None:
    """Counted from page one, the resumed walk (next page 3) would trip before its first request."""
    rows = [row(n) for n in range(7)]  # four pages of two
    with pytest.raises(FuseTripped):
        number_walk(NumberedListing(rows), checkpoint, max_pages=2)
    resumed = NumberedListing(rows)
    assert ids(number_walk(resumed, checkpoint, max_pages=2)) == ["r4", "r5", "r6"]
    assert resumed.calls == [3, 4]


def test_a_resumed_token_walk_gets_a_fresh_page_allowance(checkpoint: Checkpoint) -> None:
    rows = [row(n) for n in range(6)]  # three pages
    with pytest.raises(FuseTripped):
        token_walk(TokenListing(rows), checkpoint, max_pages=2)
    resumed = TokenListing(rows)
    assert ids(token_walk(resumed, checkpoint, max_pages=2)) == ["r4", "r5"]
    assert resumed.calls == ["t4"]


def test_a_resumed_offset_walk_gets_a_fresh_page_allowance(checkpoint: Checkpoint) -> None:
    rows = [row(n) for n in range(9)]  # three full pages, then the empty one that settles it
    with pytest.raises(FuseTripped):
        offset_walk(OffsetListing(rows), checkpoint, max_pages=2)
    resumed = OffsetListing(rows)
    assert ids(offset_walk(resumed, checkpoint, max_pages=2)) == ["r6", "r7", "r8"]
    assert resumed.calls == [(6, 3), (9, 3)]


def test_every_max_pages_setting_says_what_the_walkers_do_with_it() -> None:
    """The walkers count the pages one run fetches; the settings the operator reads must say so,
    not "pages before the pull stops" (which a resume made look as if it started where it left)."""
    from catalog_sync.plugins.mock.settings import MAX_PAGES as MOCK_MAX_PAGES

    template = REPO_ROOT / "src/catalog_sync/scaffold/templates/package/settings.py.tmpl"
    for text in (MOCK_MAX_PAGES.help, template.read_text(encoding="utf-8")):
        assert "resumed run counts again from zero" in text
