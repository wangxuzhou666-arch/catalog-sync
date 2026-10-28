"""Response-envelope helpers.

The services this tool targets wrap payloads as ``{"state": true, "data": {...}}`` and some
endpoints wrap twice (``data.data``). Both helpers fail loudly on any other shape, so a
changed API surfaces as SourceShapeError instead of as silently empty records.
"""

from __future__ import annotations

from collections.abc import Mapping

from catalog_sync.errors import SourceShapeError

_MAX_SERVER_MESSAGE = 200


def require_state_ok(payload: Mapping[str, object]) -> None:
    """Raise unless the envelope says ``state`` is exactly ``True``.

    ``state`` must be a real boolean: a truthy string or number is a different API than the one
    this was written for, and "probably fine" is how empty pages get mistaken for empty libraries.
    """
    state = payload.get("state")
    if state is True:
        return
    if state is False:
        message = payload.get("msg", payload.get("message"))
        detail = f": {message[:_MAX_SERVER_MESSAGE]}" if isinstance(message, str) else ""
        raise SourceShapeError(f"response state is false{detail}")
    raise SourceShapeError(f"response has no boolean 'state' (got {type(state).__name__})")


def peel_data(payload: Mapping[str, object], depth: int) -> Mapping[str, object]:
    """Unwrap exactly `depth` layers of ``{"data": ...}`` and return the object inside.

    The depth is fixed by the caller, never discovered by looping "while it has a data key":
    a record may legitimately own a field called ``data``, and an unbounded loop would eat it.
    """
    if depth < 1:
        raise ValueError(f"depth must be at least 1, got {depth}")
    current: Mapping[str, object] = payload
    for level in range(1, depth + 1):
        inner = current.get("data")
        if not isinstance(inner, Mapping):
            raise SourceShapeError(
                f"expected an object under 'data' at level {level} of {depth}, "
                f"got {type(inner).__name__}"
            )
        current = inner
    return current
