"""Checking what a service answered, and turning HTTP failures into the errors a run understands.

Two habits every connector needs, kept in one place so they are not re-invented per service:

* An answer is checked before anything uses it, and a failed check says *which call* it was
  (``shape_context``); a changed API then surfaces as ``SourceShapeError``, not as empty records.
* A 4xx about one item (gone, malformed) fails that item and the run goes on; a 4xx that says
  something about the credential or the service stops the run (``item_scoped``).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from catalog_sync.envelope import peel_data, require_state_ok
from catalog_sync.errors import HttpStatusError, ItemFailed, ResponseTooLarge, SourceShapeError
from catalog_sync.models import JsonObject

DETAIL_ENVELOPE_DEPTH = 2


@contextmanager
def shape_context(label: str) -> Iterator[None]:
    """Prefix a ``SourceShapeError`` raised inside the block with the call it belongs to."""
    try:
        yield
    except SourceShapeError as exc:
        raise SourceShapeError(f"{label}: {exc}") from exc


@contextmanager
def item_scoped(
    source_id: str, what: str, *, hints: Mapping[int, str] | None = None
) -> Iterator[None]:
    """Turn a failure that concerns one item into ``ItemFailed``; anything else passes.

    That is an HTTP status about the item itself (a 4xx other than 429: gone, malformed) and a
    download larger than the limit set for it. ``what`` completes "<what> answered HTTP 404";
    ``hints`` adds an explanation for a status that has a known cause (a 403 on a signed link
    means it expired).
    """
    try:
        yield
    except HttpStatusError as exc:
        if not exc.is_item_scoped:
            raise
        hint = (hints or {}).get(exc.status)
        suffix = f" ({hint})" if hint else ""
        raise ItemFailed(source_id, f"{what} answered HTTP {exc.status}{suffix}") from exc
    except ResponseTooLarge as exc:
        raise ItemFailed(source_id, f"{what}: {exc}") from exc


def parse_detail(
    payload: Mapping[str, object], *, depth: int = DETAIL_ENVELOPE_DEPTH
) -> JsonObject:
    """The record inside ``{"state": true, "data": {"data": {...}}}``, unwrapped exactly ``depth``
    times (never "until there is no more ``data``": a record may own a field called ``data``)."""
    require_state_ok(payload)
    detail = peel_data(payload, depth)
    if not detail:
        raise SourceShapeError("the detail record is empty")
    return dict(detail)
