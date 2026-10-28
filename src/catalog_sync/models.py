"""The normalised records, their value types and their JSON form.

A record is whatever its *schema* (``catalog_sync.schema``) says: ``material`` for a fabric
library, ``supplier`` for a supplier list, or a schema of your own. ``record_class`` builds the
record type of one schema; ``MaterialRecord`` is the type of the built-in ``material`` schema.

Design rules that the rest of the tool relies on:

* ``None`` means "the source did not say". A value is never invented to fill a gap; a gap that
  needs explaining gets an entry in ``issues`` instead.
* Money is ``Decimal`` in memory and a decimal string in JSON, never a float.
* Records are frozen. A sink or a mapping builds a new record rather than mutating one.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, make_dataclass
from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Self, TypeAlias, TypeVar, cast

from catalog_sync.errors import RecordFormatError
from catalog_sync.schema import (
    PROVENANCE_FIELDS as _PROVENANCE_NAMES,
)
from catalog_sync.schema import (
    EntitySchema,
    FieldKind,
    FieldSpec,
    NestedKind,
    builtin_schema,
)

JsonObject: TypeAlias = dict[str, object]
RecordKey: TypeAlias = tuple[str, str]

_CURRENCY = re.compile(r"[A-Z]{3}")
_T = TypeVar("_T")


@dataclass(frozen=True)
class Composition:
    """One fibre share. ``fiber`` is None when the source's code is not in the code table."""

    fiber: str | None
    percent: float | None
    raw: str

    def to_dict(self) -> JsonObject:
        return {"fiber": self.fiber, "percent": self.percent, "raw": self.raw}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> Composition:
        return cls(
            fiber=_opt_str(raw, "fiber"), percent=_opt_number(raw, "percent"), raw=_str(raw, "raw")
        )


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not self.amount.is_finite():
            raise ValueError(f"money amount must be finite, got {self.amount}")
        if not _CURRENCY.fullmatch(self.currency):
            raise ValueError(f"currency must be a 3-letter upper-case code, got {self.currency!r}")

    def to_dict(self) -> JsonObject:
        return {"amount": str(self.amount), "currency": self.currency}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> Money:
        # The amount travels as a string: a JSON number would round-trip through float.
        return cls(amount=Decimal(_str(raw, "amount")), currency=_str(raw, "currency"))


@dataclass(frozen=True)
class ImageRef:
    """A picture belonging to a record. ``local_path`` is relative to the run snapshot."""

    role: str
    source_url: str | None = None
    local_path: str | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None

    def to_dict(self) -> JsonObject:
        return {
            "role": self.role,
            "source_url": self.source_url,
            "local_path": self.local_path,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ImageRef:
        return cls(
            role=_str(raw, "role"),
            source_url=_opt_str(raw, "source_url"),
            local_path=_opt_str(raw, "local_path"),
            sha256=_opt_str(raw, "sha256"),
            width=_opt_int(raw, "width"),
            height=_opt_int(raw, "height"),
            size_bytes=_opt_int(raw, "size_bytes"),
        )


# --- records ------------------------------------------------------------------------------------


class Record:
    """What every schema's record class has in common. Not instantiated itself: `record_class`
    builds, for one schema, a frozen dataclass with a field per column of that schema.

    In memory a value has its natural type (a ``Money``, a tuple of ``ImageRef``, an aware
    ``datetime``); ``to_dict`` gives the JSON form sinks and files use, and ``from_dict`` is its
    strict inverse.
    """

    schema: ClassVar[EntitySchema]

    source: str
    source_id: str
    issues: tuple[str, ...]
    source_version: str | None
    raw_ref: str | None
    fetched_at: datetime | None
    normalized_at: datetime | None
    schema_version: int

    def __post_init__(self) -> None:
        for spec in self.schema.columns:
            if spec.kind is not FieldKind.TIMESTAMP:
                continue
            stamp = getattr(self, spec.name)
            if stamp is not None and stamp.tzinfo is None:
                raise ValueError(f"{spec.name} must be timezone-aware")

    @property
    def key(self) -> RecordKey:
        return (self.source, self.source_id)

    def to_dict(self) -> JsonObject:
        encoded: JsonObject = {"source": self.source, "source_id": self.source_id}
        for spec in self.schema.columns:
            encoded[spec.name] = _encode(spec, getattr(self, spec.name))
        return encoded

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> Self:
        """Strict inverse of to_dict: unknown or missing keys are errors, not defaults."""
        try:
            return cls._from_dict(raw)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            # Decimal() signals bad input with InvalidOperation, an ArithmeticError.
            raise RecordFormatError(f"invalid record: {type(exc).__name__}: {exc}") from exc

    @classmethod
    def _from_dict(cls, raw: Mapping[str, object]) -> Self:
        unknown = set(raw) - cls.schema.field_names
        if unknown:
            raise ValueError(f"unknown fields {sorted(unknown)}")
        schema_version = _opt_int(raw, "schema_version")
        if schema_version != cls.schema.schema_version:
            raise ValueError(f"unsupported schema_version {schema_version!r}")
        values = {spec.name: _decode(spec, raw) for spec in cls.schema.columns}
        factory: Callable[..., Self] = cls
        return factory(source=_str(raw, "source"), source_id=_str(raw, "source_id"), **values)


@functools.cache
def record_class(schema: EntitySchema) -> type[Record]:
    """The record type of `schema`. One class per distinct schema, so that records of equal
    schemas compare equal wherever the schema was loaded from."""
    columns: list[tuple[str, type] | tuple[str, type, object]] = [
        ("source", str),
        ("source_id", str),
    ]
    columns += [(spec.name, object, _default(schema, spec)) for spec in schema.columns]
    name = "".join(part.capitalize() for part in schema.entity.split("_")) + "Record"
    made = make_dataclass(name, columns, bases=(Record,), frozen=True, namespace={"schema": schema})
    return cast("type[Record]", made)


def new_record(
    schema: EntitySchema, source: str, source_id: str, values: Mapping[str, object]
) -> Record:
    """A record of `schema`; fields `values` does not mention take their defaults."""
    factory: Callable[..., Record] = record_class(schema)
    return factory(source=source, source_id=source_id, **values)


def _default(schema: EntitySchema, spec: FieldSpec) -> object:
    """What a field holds when nothing was said: a list-valued field is empty, not None."""
    if spec.name == "schema_version":
        return field(default=schema.schema_version)
    if spec.nested is NestedKind.OBJECT:
        return field(default_factory=dict)
    if spec.nested in (NestedKind.TEXTS, NestedKind.COMPOSITION, NestedKind.IMAGES):
        return field(default=())
    return field(default=None)


MATERIAL_SCHEMA = builtin_schema("material")
# The record type of the built-in `material` schema, for code that only ever deals with materials.
MaterialRecord = record_class(MATERIAL_SCHEMA)

# The material preset's field sets, for sinks and adapters written for materials only.
ALL_FIELDS: frozenset[str] = MATERIAL_SCHEMA.field_names
OWNABLE_FIELDS: frozenset[str] = MATERIAL_SCHEMA.ownable
PROVENANCE_FIELDS: frozenset[str] = frozenset(_PROVENANCE_NAMES)
LOADED_PROVENANCE_FIELDS: frozenset[str] = MATERIAL_SCHEMA.loaded_provenance


def is_empty_json(value: object) -> bool:
    """True for null, [] and {}: the JSON forms of "the source gave nothing".

    Used by `keep_existing_if_null`. 0, 0.0 and "" are values, not gaps; deciding that a zero
    weight means "unknown" is the normaliser's job, and it records an issue when it does.
    """
    return value is None or value == [] or value == {}


# --- JSON form ----------------------------------------------------------------------------------


def _expect(wanted: type[_T], value: object) -> _T:
    if not isinstance(value, wanted):
        raise TypeError(f"expected {wanted.__name__}, got {type(value).__name__}")
    return value


def _encode(spec: FieldSpec, value: object) -> object:
    if spec.nested is None:
        return _iso(value) if spec.kind is FieldKind.TIMESTAMP else value
    return _NESTED_ENCODERS[spec.nested](value)


def _iso(value: object) -> str | None:
    return None if value is None else _expect(datetime, value).isoformat()


def _encode_texts(value: object) -> object:
    return list(_expect(tuple, value))


def _encode_money(value: object) -> object:
    return None if value is None else _expect(Money, value).to_dict()


def _encode_composition(value: object) -> object:
    return [_expect(Composition, part).to_dict() for part in _expect(tuple, value)]


def _encode_images(value: object) -> object:
    return [_expect(ImageRef, image).to_dict() for image in _expect(tuple, value)]


def _encode_object(value: object) -> object:
    if not isinstance(value, Mapping):
        raise TypeError(f"expected a mapping, got {type(value).__name__}")
    return dict(value)


_NESTED_ENCODERS: Mapping[NestedKind, Callable[[object], object]] = {
    NestedKind.TEXTS: _encode_texts,
    NestedKind.MONEY: _encode_money,
    NestedKind.COMPOSITION: _encode_composition,
    NestedKind.IMAGES: _encode_images,
    NestedKind.OBJECT: _encode_object,
}


def _decode(spec: FieldSpec, raw: Mapping[str, object]) -> object:
    if spec.nested is None:
        return _SCALAR_DECODERS[spec.kind](raw, spec.name)
    return _NESTED_DECODERS[spec.nested](raw, spec.name)


def _decode_money(raw: Mapping[str, object], key: str) -> Money | None:
    body = _opt_object(raw, key)
    return None if body is None else Money.from_dict(body)


def _decode_composition(raw: Mapping[str, object], key: str) -> tuple[Composition, ...]:
    return tuple(Composition.from_dict(item) for item in _objects(raw, key))


def _decode_images(raw: Mapping[str, object], key: str) -> tuple[ImageRef, ...]:
    return tuple(ImageRef.from_dict(item) for item in _objects(raw, key))


def _decode_object(raw: Mapping[str, object], key: str) -> dict[str, object]:
    return dict(_object(raw, key))


_SCALAR_DECODERS: Mapping[FieldKind, Callable[[Mapping[str, object], str], object]] = {
    FieldKind.TEXT: lambda raw, key: _opt_str(raw, key),
    FieldKind.INTEGER: lambda raw, key: _opt_int(raw, key),
    FieldKind.REAL: lambda raw, key: _opt_number(raw, key),
    FieldKind.TIMESTAMP: lambda raw, key: _opt_datetime(raw, key),
}
_NESTED_DECODERS: Mapping[NestedKind, Callable[[Mapping[str, object], str], object]] = {
    NestedKind.TEXTS: lambda raw, key: _strs(raw, key),
    NestedKind.MONEY: _decode_money,
    NestedKind.COMPOSITION: _decode_composition,
    NestedKind.IMAGES: _decode_images,
    NestedKind.OBJECT: _decode_object,
}


def _as_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}: expected str, got {type(value).__name__}")
    return value


def _as_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label}: expected object, got {type(value).__name__}")
    return value


def _as_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label}: expected list, got {type(value).__name__}")
    return value


def _str(raw: Mapping[str, object], key: str) -> str:
    return _as_str(raw[key], key)


def _opt_str(raw: Mapping[str, object], key: str) -> str | None:
    return None if raw[key] is None else _as_str(raw[key], key)


def _opt_int(raw: Mapping[str, object], key: str) -> int | None:
    value = raw[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key}: expected int, got {type(value).__name__}")
    return value


def _opt_number(raw: Mapping[str, object], key: str) -> float | None:
    value = raw[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key}: expected number, got {type(value).__name__}")
    return float(value)


def _opt_datetime(raw: Mapping[str, object], key: str) -> datetime | None:
    text = _opt_str(raw, key)
    return None if text is None else datetime.fromisoformat(text)


def _object(raw: Mapping[str, object], key: str) -> Mapping[str, object]:
    return _as_object(raw[key], key)


def _opt_object(raw: Mapping[str, object], key: str) -> Mapping[str, object] | None:
    return None if raw[key] is None else _as_object(raw[key], key)


def _objects(raw: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    return [_as_object(item, key) for item in _as_list(raw[key], key)]


def _strs(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    return tuple(_as_str(item, key) for item in _as_list(raw[key], key))
