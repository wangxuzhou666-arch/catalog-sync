"""Writing a config file as TOML, for the few value types a config holds.

The standard library reads TOML (``tomllib``) but cannot write it, and one small function is
cheaper than a dependency. It writes tables with comments, which is the point: the file is meant to
be read and edited by a person afterwards. Supported values: strings, integers, floats, booleans,
dates and lists of those. Anything else is a bug in the caller and raises.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_DELETE = "\x7f"  # the one control character json.dumps leaves alone and TOML forbids


@dataclass(frozen=True)
class Table:
    """``[header]`` with `values` in order. `comments` maps a key to a comment written above it;
    `note` is written above the header."""

    header: Sequence[str]
    values: Sequence[tuple[str, object]]
    comments: dict[str, str] = field(default_factory=dict)
    note: str = ""


def dumps(tables: Sequence[Table], *, preamble: str = "") -> str:
    blocks = [_comment(preamble).rstrip("\n")] if preamble else []
    blocks += [_table(table) for table in tables]
    return "\n\n".join(blocks) + "\n"


def _table(table: Table) -> str:
    lines = [_comment(table.note).rstrip("\n")] if table.note else []
    lines.append("[" + ".".join(_key(part) for part in table.header) + "]")
    for name, value in table.values:
        if name in table.comments:
            lines.append(_comment(table.comments[name]).rstrip("\n"))
        lines.append(f"{_key(name)} = {_value(value)}")
    return "\n".join(lines)


def _comment(text: str) -> str:
    return "".join(f"# {line}".rstrip() + "\n" for line in text.splitlines())


def _key(name: str) -> str:
    return name if _BARE_KEY.fullmatch(name) else _string(name)


def _value(value: object) -> str:
    if isinstance(value, bool):  # before int: bool is an int
        return "true" if value else "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("TOML has no NaN or infinity")
        return repr(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise ValueError(f"cannot write a {type(value).__name__} as a TOML value")


def _string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False).replace(_DELETE, "\\u007f")
