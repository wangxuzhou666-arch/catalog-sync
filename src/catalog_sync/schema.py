"""Entity schemas: what a record of one kind of thing (a material, a supplier) consists of.

A schema is a small YAML file::

    version: 1                 # the format of this file
    entity: supplier
    schema_version: 1          # written into every record; bump it when a field changes meaning
    key: [source, source_id]
    fields:
      name: {kind: text}
      rating: {kind: real, parse: number}
      certifications: {kind: json, of: texts}

``kind`` says how a sink stores the field (``FieldKind``). A ``json`` field also says what it
holds (``of``, a ``NestedKind``), because a sink only needs to know "JSON" but the record has to
turn the value into JSON and back, and a mapping has to know what parser may fill it. ``parse``
optionally names the parser a mapping rule uses when it does not name one itself.

Every record also carries, whatever the schema says, ``source`` and ``source_id`` (its identity),
``extra`` (values a mapping passes through untouched) and the provenance fields the tool writes
(``issues``, ``source_version``, ``raw_ref``, ``fetched_at``, ``normalized_at``,
``schema_version``). A schema may not declare those names.

``material`` is the preset the first release shipped with; ``builtin_schema("material")`` reads it
from the package.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, TypeVar

from catalog_sync.errors import ConfigError
from catalog_sync.yamlio import load_yaml_file

_FORMAT_VERSION = 1
_E = TypeVar("_E", bound=StrEnum)
_NAME = re.compile(r"[a-z][a-z0-9_]{0,40}")
_PRESETS = Path(__file__).parent / "schemas"
BUILTIN_SCHEMAS: Final = ("material",)

IDENTITY_FIELDS: Final = ("source", "source_id")
KEY_FIELDS: Final[frozenset[str]] = frozenset(IDENTITY_FIELDS)
EXTRA_FIELD: Final = "extra"
PROVENANCE_FIELDS: Final = (
    "issues",
    "source_version",
    "raw_ref",
    "fetched_at",
    "normalized_at",
    "schema_version",
)
# `normalized_at` stays in the normalised file but is not loaded: it changes on every normalise,
# so owning it would make every row "updated" on every run, and a load report that always says
# "updated" cannot tell a change from a re-run.
UNLOADED_PROVENANCE_FIELDS: Final = frozenset({"normalized_at"})
_RESERVED = KEY_FIELDS | {EXTRA_FIELD, *PROVENANCE_FIELDS}


class FieldKind(StrEnum):
    """How a field's value is stored: native column types for scalars, JSON text for the rest."""

    TEXT = "text"
    INTEGER = "integer"
    REAL = "real"
    JSON = "json"  # lists and objects; also what a hashed or otherwise replaced value may be
    TIMESTAMP = "timestamp"  # an ISO 8601 string with a UTC offset; a point in time, not a label


class NestedKind(StrEnum):
    """What a ``json`` field holds, which fixes how its value is encoded, decoded and filled."""

    TEXTS = "texts"  # a list of strings
    MONEY = "money"  # {"amount": "12.50", "currency": "USD"}
    COMPOSITION = "composition"  # a list of fibre shares
    IMAGES = "images"  # a list of pictures
    OBJECT = "object"  # any JSON object, passed through


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: FieldKind
    nested: NestedKind | None = None  # set exactly when `kind` is JSON
    parser: str | None = None  # what a mapping rule uses when it names no parser

    def __post_init__(self) -> None:
        if (self.kind is FieldKind.JSON) != (self.nested is not None):
            raise ValueError(f"field {self.name}: 'of' goes with kind json, and only with it")


_EXTRA = FieldSpec(EXTRA_FIELD, FieldKind.JSON, NestedKind.OBJECT)
_PROVENANCE = (
    FieldSpec("issues", FieldKind.JSON, NestedKind.TEXTS),
    FieldSpec("source_version", FieldKind.TEXT),
    FieldSpec("raw_ref", FieldKind.TEXT),
    FieldSpec("fetched_at", FieldKind.TIMESTAMP),
    FieldSpec("normalized_at", FieldKind.TIMESTAMP),
    FieldSpec("schema_version", FieldKind.INTEGER),
)


@dataclass(frozen=True)
class EntitySchema:
    entity: str
    schema_version: int
    fields: tuple[FieldSpec, ...]  # the declared fields, in file order
    # Derived once: every field after the identity, in record order, and how each is stored.
    # `kinds` is the very object sinks receive in a Batch, so it must not be rebuilt per call.
    columns: tuple[FieldSpec, ...] = field(init=False, repr=False, compare=False)
    kinds: Mapping[str, FieldKind] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        columns = (*self.fields, _EXTRA, *_PROVENANCE)
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "kinds", {spec.name: spec.kind for spec in columns})

    @property
    def field_names(self) -> frozenset[str]:
        """Every key of a record's JSON form."""
        return KEY_FIELDS | frozenset(self.kinds)

    @property
    def ownable(self) -> frozenset[str]:
        """What a sink batch may claim ownership of: everything except the record's identity."""
        return frozenset(self.kinds)

    @property
    def mappable(self) -> frozenset[str]:
        """The fields a mapping rule may fill: the declared ones. ``extra`` is filled through
        ``extra.<namespace>.<name>`` rules, and provenance is written by the tool."""
        return frozenset(spec.name for spec in self.fields)

    @property
    def loaded_provenance(self) -> frozenset[str]:
        return frozenset(PROVENANCE_FIELDS) - UNLOADED_PROVENANCE_FIELDS

    def spec(self, name: str) -> FieldSpec | None:
        return next((spec for spec in self.columns if spec.name == name), None)

    def to_json(self) -> dict[str, object]:
        """The schema as a document `parse_schema` accepts; embedded in normalised files so that
        a load needs nothing but the file."""
        return {
            "version": _FORMAT_VERSION,
            "entity": self.entity,
            "schema_version": self.schema_version,
            "key": list(IDENTITY_FIELDS),
            "fields": {spec.name: _field_json(spec) for spec in self.fields},
        }


def _field_json(spec: FieldSpec) -> dict[str, str]:
    body = {"kind": spec.kind.value}
    if spec.nested is not None:
        body["of"] = spec.nested.value
    if spec.parser is not None:
        body["parse"] = spec.parser
    return body


# --- reading ------------------------------------------------------------------------------------


@functools.cache
def builtin_schema(name: str) -> EntitySchema:
    """A schema shipped inside the package (today only ``material``)."""
    return load_schema_file(_PRESETS / f"{name}.yaml")


def load_schema_file(path: Path) -> EntitySchema:
    return parse_schema(load_yaml_file(path, what="schema"), path.name)


def parse_schema(raw: object, name: str) -> EntitySchema:
    """Validate a loaded schema document. `name` is what error messages call it."""
    label = f"schema {name}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a YAML mapping")
    unknown = sorted(set(raw) - {"version", "entity", "schema_version", "key", "fields"})
    if unknown:
        raise ConfigError(f"{label}: unknown top-level key(s) {unknown}")
    if raw.get("version") != _FORMAT_VERSION:
        raise ConfigError(f"{label}: 'version' must be {_FORMAT_VERSION}")
    entity = raw.get("entity")
    if not isinstance(entity, str) or not _NAME.fullmatch(entity):
        raise ConfigError(f"{label}: 'entity' must match {_NAME.pattern}")
    revision = raw.get("schema_version")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ConfigError(f"{label}: 'schema_version' must be a whole number >= 1")
    if raw.get("key") != list(IDENTITY_FIELDS):
        # Sinks and the pull state key every row by (source, source_id); the schema states it so
        # that a file written for a different key fails here rather than inside a sink.
        raise ConfigError(f"{label}: 'key' must be {list(IDENTITY_FIELDS)}")
    bodies = raw.get("fields")
    if not isinstance(bodies, dict) or not bodies:
        raise ConfigError(f"{label}: 'fields' must list at least one field")
    specs = tuple(_field(str(key), body, label) for key, body in bodies.items())
    return EntitySchema(entity=entity, schema_version=revision, fields=specs)


def _field(name: str, body: object, label: str) -> FieldSpec:
    where = f"{label}: field {name!r}"
    if not _NAME.fullmatch(name):
        raise ConfigError(f"{where} is not a plain lower-case name ({_NAME.pattern})")
    if name in _RESERVED:
        raise ConfigError(f"{where} is reserved: every record has it already")
    if not isinstance(body, dict):
        raise ConfigError(f"{where}: write it as {{kind: text}}")
    stray = sorted(set(body) - {"kind", "of", "parse"})
    if stray:
        raise ConfigError(f"{where}: unknown key(s) {stray}")
    kind = _enum(FieldKind, body.get("kind"), f"{where}: 'kind'")
    nested = None
    if "of" in body:
        nested = _enum(NestedKind, body["of"], f"{where}: 'of'")
    if (kind is FieldKind.JSON) != (nested is not None):
        raise ConfigError(f"{where}: 'of' goes with kind json, and a json field needs it")
    parser = body.get("parse")
    if parser is not None and not isinstance(parser, str):
        raise ConfigError(f"{where}: 'parse' must name a parser")
    return FieldSpec(name, kind, nested, parser)


def _enum(enum_type: type[_E], value: object, what: str) -> _E:
    members = {member.value: member for member in enum_type}
    if isinstance(value, str) and value in members:
        return members[value]
    raise ConfigError(f"{what} must be one of {', '.join(members)}, got {value!r}")
