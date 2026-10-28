"""SQL identifier rules shared by the SQL sinks.

Table and column names cannot be bound as query parameters, so the SQL sinks splice them into the
statement text. That is only safe for names that cannot carry syntax, hence this one pattern.
"""

from __future__ import annotations

import re

from catalog_sync.errors import SinkError

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def plain_identifier(name: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(name):
        raise SinkError(f"{label} {name!r} is not a plain SQL identifier")
    return name
