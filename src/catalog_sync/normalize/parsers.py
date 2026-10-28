"""Value parsers: the small functions a mapping rule can name with ``parse:``.

A parser turns one raw value (usually a string such as ``"280G/M2"``) into a typed one, or says
why it cannot. It never guesses: a value that does not fit raises ``ParseFailure`` and the engine
turns that into ``None`` plus an issue on the record. ``PARSERS`` is a plain dictionary; a parser
is a function with the signature ``(value, options) -> Parsed``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, TypeAlias

from catalog_sync.models import Composition, Money
from catalog_sync.normalize.codetables import CodeTable

Produces: TypeAlias = Literal[
    "text", "texts", "number", "integer", "timestamp", "money", "composition", "any"
]

_NUMBER = r"\d+(?:\.\d+)?"
_SIGNED_NUMBER = re.compile(rf"-?{_NUMBER}")
_WHOLE_NUMBER = re.compile(r"[+-]?\d+")
# A weight is a number, optionally followed by a grams-per-square-metre unit. Anything else
# ("5 oz/yd2") is refused rather than read as if the unit did not matter.
_SQUARED = f"(?:2|{chr(0xB2)})"  # "2" or the superscript two; chr() keeps the source ASCII
_GSM = re.compile(
    rf"(?P<n>{_NUMBER})\s*(?:gsm|g\s*/\s*(?:m|sqm)\s*{_SQUARED}?|gr\s*/\s*m\s*{_SQUARED}?|g)?",
    re.IGNORECASE,
)
_INCH_UNIT = r'(?:"|in|inch|inches)'
_WIDTH_INCH_RANGE = re.compile(rf"(?P<a>{_NUMBER})\s*/\s*(?P<b>{_NUMBER})\s*{_INCH_UNIT}", re.I)
_WIDTH_INCH = re.compile(rf"(?P<n>{_NUMBER})\s*{_INCH_UNIT}", re.IGNORECASE)
_WIDTH_CM = re.compile(rf"(?P<n>{_NUMBER})\s*(?:cm)?", re.IGNORECASE)
_MONEY = re.compile(_NUMBER)
_CM_PER_INCH = Decimal("2.54")
# Separators between shares: ASCII ones plus the fullwidth comma (U+FF0C) and the ideographic comma
# (U+3001). Built with chr() so this file contains no CJK characters.
_SEPARATORS = "/+,;" + chr(0xFF0C) + chr(0x3001)
_SHARE = re.compile(
    rf"\s*(?P<number>{_NUMBER})\s*(?P<percent>%?)\s*(?P<code>[^\d%{re.escape(_SEPARATORS)}]*)"
)
# A yarn count has no percent sign: "30S", "75D/72F" (denier / filament), "40S*40S", "21Sx21S",
# "40S/2" (ply). The trailing lookahead keeps "40SP" (40 of fibre SP) from reading as "40S" + "P".
_YARN_UNIT = rf"{_NUMBER}(?:\s*[SDF])?"
_YARN_COUNT = re.compile(
    rf"\s*(?P<count>{_NUMBER}\s*[SDF](?:\s*[*x{chr(0xD7)}/]\s*{_YARN_UNIT})*)(?![A-Za-z])",
    re.IGNORECASE,
)


class ParseFailure(ValueError):
    """The value is not in a form this parser understands."""


@dataclass(frozen=True)
class Parsed:
    value: object  # None, or an empty text / tuple, means "nothing usable in this value"
    issues: tuple[str, ...] = ()


Parser: TypeAlias = Callable[[object, Mapping[str, object]], Parsed]


@dataclass(frozen=True)
class ParserSpec:
    run: Parser
    produces: Produces
    options: frozenset[str] = frozenset()  # rule keys this parser reads
    required: frozenset[str] = frozenset()  # the subset of `options` that must be present


# --- text ---------------------------------------------------------------------------------------


def parse_text(value: object, _options: Mapping[str, object]) -> Parsed:
    return Parsed(_as_text(value))


def parse_list(value: object, _options: Mapping[str, object]) -> Parsed:
    items = value if isinstance(value, list) else [value]
    return Parsed(_unique_texts(items))


def parse_split(value: object, options: Mapping[str, object]) -> Parsed:
    separators = str(options.get("separators", ",;"))
    if isinstance(value, list):
        return Parsed(_unique_texts(value))
    pattern = "[" + re.escape(separators) + "]"
    return Parsed(_unique_texts(re.split(pattern, _as_text(value) or "")))


def parse_raw(value: object, _options: Mapping[str, object]) -> Parsed:
    return Parsed(value)


def _as_text(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ParseFailure(f"expected text, got {type(value).__name__}")
    return str(value).strip() or None


def _unique_texts(items: Sequence[object]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in items:
        text = _as_text(item)
        if text is not None:
            seen[text] = None
    return tuple(seen)


# --- numbers ------------------------------------------------------------------------------------


def parse_gsm(value: object, _options: Mapping[str, object]) -> Parsed:
    """Grams per square metre: ``"280"``, ``"280G/M2"``, ``"0GSM"``. Zero is returned as zero."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Parsed(float(value))
    match = _GSM.fullmatch(_require_text(value))
    if match is None:
        raise ParseFailure("not a weight in grams per square metre")
    return Parsed(float(match["n"]))


def parse_width_cm(value: object, _options: Mapping[str, object]) -> Parsed:
    """Fabric width in centimetres from ``"150"``, ``"150CM"``, ``'56"'`` or ``'57/58"'``.

    An inch range takes its midpoint. A bare number is centimetres. Results are rounded to a
    tenth of a centimetre.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Parsed(round(float(value), 1))
    text = _require_text(value)
    inch_range = _WIDTH_INCH_RANGE.fullmatch(text)
    if inch_range is not None:
        midpoint = (Decimal(inch_range["a"]) + Decimal(inch_range["b"])) / 2
        return Parsed(round(float(midpoint * _CM_PER_INCH), 1))
    inches = _WIDTH_INCH.fullmatch(text)
    if inches is not None:
        return Parsed(round(float(Decimal(inches["n"]) * _CM_PER_INCH), 1))
    centimetres = _WIDTH_CM.fullmatch(text)
    if centimetres is None:
        raise ParseFailure("not a width in centimetres or inches")
    return Parsed(round(float(centimetres["n"]), 1))


def parse_number(value: object, _options: Mapping[str, object]) -> Parsed:
    """A plain number, written as a number or as text such as ``"4.5"`` or ``"-2"``; no unit."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ParseFailure("not a finite number")
        return Parsed(float(value))
    text = _require_text(value)
    if _SIGNED_NUMBER.fullmatch(text) is None:
        raise ParseFailure("not a plain number")
    return Parsed(float(text))


def parse_integer(value: object, _options: Mapping[str, object]) -> Parsed:
    """A whole number: ``14``, ``"14"``, ``14.0``. ``14.5`` is refused rather than rounded."""
    if isinstance(value, bool):
        raise ParseFailure("expected a whole number, got a boolean")
    if isinstance(value, int):
        return Parsed(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ParseFailure("not a whole number")
        return Parsed(int(value))
    text = _require_text(value)
    if _WHOLE_NUMBER.fullmatch(text) is None:
        raise ParseFailure("not a whole number")
    return Parsed(int(text))


def parse_timestamp(value: object, _options: Mapping[str, object]) -> Parsed:
    """An ISO 8601 date and time *with* an offset (``2026-03-01T12:00:00+02:00``), returned in UTC.

    Without an offset the instant is unknown, and a point in time must not depend on the machine
    that read it, so such a value is refused rather than assumed to be UTC. A value that has one is
    converted to UTC (``2026-03-01T10:00:00+00:00``): ISO strings only sort chronologically when
    they all carry the same offset, and the sinks rely on that.
    """
    text = _require_text(value)
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ParseFailure("not an ISO 8601 date and time") from exc
    if stamp.tzinfo is None:
        raise ParseFailure("the time has no UTC offset")
    return Parsed(stamp.astimezone(UTC))


def parse_money(value: object, options: Mapping[str, object]) -> Parsed:
    """A non-negative decimal amount; the currency is the mapping's statement, not the source's."""
    currency = str(options["currency"])
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ParseFailure(f"expected an amount, got {type(value).__name__}")
    text = str(value).strip()
    if _MONEY.fullmatch(text) is None:
        raise ParseFailure("not a plain decimal amount")
    return Parsed(Money(Decimal(text), currency))


def _require_text(value: object) -> str:
    if not isinstance(value, str):
        raise ParseFailure(f"expected text, got {type(value).__name__}")
    return value.strip()


# --- composition --------------------------------------------------------------------------------


def parse_composition(value: object, options: Mapping[str, object]) -> Parsed:
    """``"60%C/25%P/15%N"`` into shares, resolving each code through the rule's code table.

    * A code the table does not list gives ``fiber=None`` and the issue
      ``composition:unknown_code:<code>``; no name is ever guessed.
    * A yarn count is a percent-less number followed by S, D or F (``30S``, ``75D/72F``,
      ``40S*40S``, ``21Sx21S``): it is not a share of a fibre, so it is left out of the shares and
      noted as ``composition:yarn_count_ignored:<count>``. Shares always carry a percent sign when
      a yarn count could be mistaken for them.
    * Shares that add up to more than 100 cannot all be right, and which one is wrong is unknowable:
      the whole composition becomes ``None`` with the issue ``composition:sum_over_100`` (the source
      text stays in the record's ``composition_raw``).
    * The whole string must be consumed. Text before the first number, or a number with no code,
      makes the string unparseable.
    """
    table = options["table"]
    if not isinstance(table, CodeTable):
        raise TypeError("a composition rule must be given its code table by the mapping loader")
    text = _require_text(value)
    shares: list[Composition] = []
    issues: list[str] = []
    total = Decimal(0)
    position = 0
    while position < len(text):
        yarn = _YARN_COUNT.match(text, position)
        if yarn is not None:
            issues.append(f"composition:yarn_count_ignored:{yarn['count']}")
            position = _skip_separators(text, yarn.end())
            continue
        match = _SHARE.match(text, position)
        code = match["code"].strip() if match else ""
        if match is None or not code:
            raise ParseFailure("not a list of 'percent code' shares")
        position = _skip_separators(text, match.end())
        fiber = table.lookup(code)
        if fiber is None:
            issues.append(f"composition:unknown_code:{code}")
        total += Decimal(match["number"])
        shares.append(
            Composition(fiber=fiber, percent=float(match["number"]), raw=match.group(0).strip())
        )
    if total > 100:
        issues.append("composition:sum_over_100")
        return Parsed(None, tuple(dict.fromkeys(issues)))
    return Parsed(tuple(shares), tuple(dict.fromkeys(issues)))


def _skip_separators(text: str, position: int) -> int:
    while position < len(text) and (text[position] in _SEPARATORS or text[position].isspace()):
        position += 1
    return position


PARSERS: dict[str, ParserSpec] = {
    "text": ParserSpec(parse_text, "text"),
    "list": ParserSpec(parse_list, "texts"),
    "split": ParserSpec(parse_split, "texts", options=frozenset({"separators"})),
    "gsm": ParserSpec(parse_gsm, "number", options=frozenset({"zero_is_missing"})),
    "width_cm": ParserSpec(parse_width_cm, "number", options=frozenset({"zero_is_missing"})),
    "number": ParserSpec(parse_number, "number", options=frozenset({"zero_is_missing"})),
    "integer": ParserSpec(parse_integer, "integer", options=frozenset({"zero_is_missing"})),
    "timestamp": ParserSpec(parse_timestamp, "timestamp"),
    "money": ParserSpec(
        parse_money,
        "money",
        options=frozenset({"currency", "zero_is_missing"}),
        required=frozenset({"currency"}),
    ),
    # The rule names its code table with `scope` (an installed plugin's table) or `table_file`; the
    # mapping loader resolves that into the `table` option this parser reads.
    "composition": ParserSpec(
        parse_composition, "composition", options=frozenset({"scope", "table_file"})
    ),
    "raw": ParserSpec(parse_raw, "any"),
}
