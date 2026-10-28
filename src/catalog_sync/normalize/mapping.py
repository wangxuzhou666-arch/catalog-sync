"""Mapping files: which raw field feeds which record field, and how.

A mapping is YAML with one explicit rule per record field it owns::

    version: 1
    source: mock.materials
    schema: material                         # the record type; defaults to `material`
    fields:
      name:
        from: [detail.name, list.name]       # the first non-empty value wins
      weight_gsm:
        from: [detail.weight]
        parse: gsm
        zero_is_missing: true                # a zero from the source means "not filled in"
      composition:
        from: [detail.composition]
        parse: composition
        scope: mock.saas/materials           # the code table; scopes are never merged
      folder_paths:
        from: [context.folder_paths]
        on_null: keep_existing               # an empty value keeps what a sink already holds
      supplier:
        from: [detail.some_field]
        sensitive: true                      # sinks drop (default), hash or keep it

A path with an index such as ``thumbs[0]`` cannot sit in a one-line ``[a, b]`` list (YAML takes
the bracket as the end of the list); write it as a block list (``- list.thumbs[0].path``) or
quote it.

Values are read from three places: ``list.<path>`` (the listing row exactly as the service
returned it), ``detail.<path>`` (the detail record) and ``context.<path>`` (what the walk knew,
such as folder paths). The two payloads are never merged into one object: neither is a superset
of the other, and an empty value in one must not wipe a value in the other. A rule that lists
several sources picks the first that has something to say.

The record fields a mapping lists are the fields it *owns*; every other field of a stored record
is left alone by a load. Passing a value through unparsed is possible with ``extra.<namespace>.
<name>`` keys, which land in the record's ``extra`` dictionary.

``schema`` says what kind of record the mapping fills: the name of the built-in ``material`` schema,
of one a plugin registers (``supplier``), or a path to a schema file next to the mapping. A rule
may only target a field the schema declares; which parser fills a field, and what it must produce,
follows from the schema's type for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias, cast

from catalog_sync import registry
from catalog_sync.errors import ConfigError
from catalog_sync.normalize.codetables import CodeTable, load_table_file, table_for
from catalog_sync.normalize.parsers import PARSERS, Produces
from catalog_sync.schema import (
    BUILTIN_SCHEMAS,
    EntitySchema,
    FieldKind,
    FieldSpec,
    NestedKind,
    builtin_schema,
    load_schema_file,
)
from catalog_sync.snapshot import canonical_json, sha256_hex
from catalog_sync.sources.base import RawBundle
from catalog_sync.yamlio import load_yaml_file

Scope: TypeAlias = Literal["list", "detail", "context"]
OnNull: TypeAlias = Literal["overwrite", "keep_existing"]

_PATH_PART = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
_PATH = r"[A-Za-z0-9_-]+(?:\[\d+\])*(?:\.[A-Za-z0-9_-]+(?:\[\d+\])*)*"
_SOURCE_REF = re.compile(rf"(?P<scope>list|detail|context)\.(?P<path>{_PATH})")
_EXTRA_KEY = re.compile(r"extra\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_FORMAT_VERSION = 1
_COMMON_KEYS = frozenset({"from", "parse", "on_null", "sensitive"})
_IMAGE_KEYS = frozenset({"from", "role", "roles", "on_null", "sensitive"})
_ON_NULL = ("overwrite", "keep_existing")

_DEFAULT_SCHEMA = "material"  # what a mapping without a `schema:` line means (the 0.1 behaviour)
# What a rule for a field of each type must produce, and the parser used when the rule names none
# (a schema field may name its own default with `parse:`).
_PRODUCES: Mapping[FieldKind | NestedKind, tuple[Produces, str]] = {
    FieldKind.TEXT: ("text", "text"),
    FieldKind.INTEGER: ("integer", "integer"),
    FieldKind.REAL: ("number", "number"),
    FieldKind.TIMESTAMP: ("timestamp", "timestamp"),
    NestedKind.TEXTS: ("texts", "list"),
    NestedKind.MONEY: ("money", "money"),
    NestedKind.COMPOSITION: ("composition", "composition"),
    NestedKind.OBJECT: ("any", "raw"),
}


def pick(value: object, path: str) -> object:
    """The value at a path like ``a.b[0].c``; None as soon as any step is absent."""
    for key, index in _PATH_PART.findall(path):
        if key:
            value = value.get(key) if isinstance(value, dict) else None
        else:
            position = int(index)
            value = value[position] if isinstance(value, list) and position < len(value) else None
        if value is None:
            return None
    return value


@dataclass(frozen=True)
class SourceRef:
    scope: Scope
    path: str

    def __str__(self) -> str:
        return f"{self.scope}.{self.path}"

    def read(self, bundle: RawBundle) -> object:
        roots: Mapping[Scope, object] = {
            "list": bundle.item.payload,
            "detail": bundle.detail,
            "context": bundle.item.context,
        }
        return pick(roots[self.scope], self.path)


@dataclass(frozen=True)
class FieldRule:
    sources: tuple[SourceRef, ...]
    parser: str | None
    options: Mapping[str, object]
    on_null: OnNull = "overwrite"
    is_sensitive: bool = False
    # Picture rules only: role name -> where its URL may come from (first non-empty wins).
    roles: Mapping[str, tuple[SourceRef, ...]] = field(default_factory=dict)

    @property
    def is_zero_missing(self) -> bool:
        return self.options.get("zero_is_missing") is True

    @property
    def is_picture(self) -> bool:
        return bool(self.roles)


@dataclass(frozen=True)
class MappingSpec:
    source: str
    schema: EntitySchema
    fields: Mapping[str, FieldRule]  # keyed by rule key: a record field or "extra.<ns>.<name>"
    digest: str  # sha256 of the schema, the rules and every code table they use

    @property
    def record_fields(self) -> frozenset[str]:
        """The record fields this mapping owns (all ``extra.*`` rules together own ``extra``)."""
        return frozenset("extra" if key.startswith("extra.") else key for key in self.fields)

    @property
    def keep_existing_if_null(self) -> frozenset[str]:
        return frozenset(
            "extra" if key.startswith("extra.") else key
            for key, rule in self.fields.items()
            if rule.on_null == "keep_existing"
        )

    @property
    def sensitive_fields(self) -> frozenset[str]:
        return frozenset(key for key, rule in self.fields.items() if rule.is_sensitive)

    @property
    def uses_detail(self) -> bool:
        return any(
            ref.scope == "detail"
            for rule in self.fields.values()
            for ref in (*rule.sources, *(r for refs in rule.roles.values() for r in refs))
        )


# --- loading ------------------------------------------------------------------------------------


def load_mapping(path: Path) -> MappingSpec:
    raw = load_yaml_file(path, what="mapping")
    return parse_mapping(raw, path.name, base_dir=path.resolve().parent)


def parse_mapping(raw: object, name: str, *, base_dir: Path) -> MappingSpec:
    """Validate a loaded mapping document. `base_dir` is where a `table_file` or a schema file
    is looked for."""
    if not isinstance(raw, dict):
        raise ConfigError(f"mapping {name} must be a YAML mapping")
    unknown = sorted(set(raw) - {"version", "source", "schema", "fields"})
    if unknown:
        raise ConfigError(f"mapping {name}: unknown top-level key(s) {unknown}")
    if raw.get("version") != _FORMAT_VERSION:
        raise ConfigError(f"mapping {name}: 'version' must be {_FORMAT_VERSION}")
    source = raw.get("source")
    if not isinstance(source, str) or not source:
        raise ConfigError(f"mapping {name}: 'source' must name the source this mapping is for")
    schema = _schema(raw.get("schema", _DEFAULT_SCHEMA), name, base_dir)
    bodies = raw.get("fields")
    if not isinstance(bodies, dict) or not bodies:
        raise ConfigError(f"mapping {name}: 'fields' must list at least one field")
    tables: list[CodeTable] = []
    fields = {
        str(key): _rule(str(key), body, name, base_dir, tables, schema)
        for key, body in bodies.items()
    }
    _check_extra_rules_agree(fields, name)
    digest = sha256_hex(
        canonical_json(
            {
                "source": source,
                "schema": schema.to_json(),
                "fields": bodies,
                "tables": [t.to_json() for t in tables],
            }
        )
    )
    return MappingSpec(source=source, schema=schema, fields=fields, digest=digest)


def _schema(ref: object, name: str, base_dir: Path) -> EntitySchema:
    if not isinstance(ref, str) or not ref:
        raise ConfigError(
            f"mapping {name}: 'schema' must be a schema name or the path of a schema file"
        )
    if ref.endswith((".yaml", ".yml")):
        return load_schema_file(base_dir / ref)
    if ref in BUILTIN_SCHEMAS:
        return builtin_schema(ref)
    installed = registry.available(registry.SCHEMAS_GROUP)
    if ref not in installed:
        known = ", ".join([*BUILTIN_SCHEMAS, *installed])
        raise ConfigError(f"mapping {name}: unknown schema {ref!r} (available: {known})")
    schema = load_schema_file(registry.schema_path(ref))
    if schema.entity != ref:
        raise ConfigError(
            f"schema {ref!r} is registered by a plugin but its file declares entity "
            f"{schema.entity!r}"
        )
    return schema


def _rule(
    key: str,
    body: object,
    name: str,
    base_dir: Path,
    tables: list[CodeTable],
    schema: EntitySchema,
) -> FieldRule:
    label = f"mapping {name}: field {key!r}"
    if key not in schema.mappable and not _EXTRA_KEY.fullmatch(key):
        raise ConfigError(
            f"{label} is not a field a mapping can fill for schema {schema.entity!r} "
            f"(its fields: {sorted(schema.mappable)}; pass-through values go under "
            "extra.<namespace>.<name>)"
        )
    if not isinstance(body, dict):
        raise ConfigError(f"{label}: the rule must be a mapping with at least 'from'")
    on_null = _on_null(body, label)
    is_sensitive = body.get("sensitive", False)
    if not isinstance(is_sensitive, bool):
        raise ConfigError(f"{label}: 'sensitive' must be true or false")
    if is_sensitive and key.startswith("extra."):
        raise ConfigError(
            f"{label}: 'sensitive' applies to whole record fields, and every extra.* value "
            "shares the one 'extra' field; map the value to a dedicated field instead"
        )
    spec = schema.spec(key)
    if spec is not None and spec.nested is NestedKind.IMAGES:
        return _image_rule(body, label, on_null, is_sensitive)
    parser_name, options = _parser(spec, body, label, base_dir, tables)
    return FieldRule(
        sources=_sources(body.get("from"), label),
        parser=parser_name,
        options=options,
        on_null=on_null,
        is_sensitive=is_sensitive,
    )


def _on_null(body: dict[str, object], label: str) -> OnNull:
    value = body.get("on_null", "overwrite")
    if value not in _ON_NULL:
        raise ConfigError(f"{label}: 'on_null' must be one of {', '.join(_ON_NULL)}")
    return cast("OnNull", value)


def _parser(
    field_spec: FieldSpec | None,
    body: dict[str, object],
    label: str,
    base_dir: Path,
    tables: list[CodeTable],
) -> tuple[str, dict[str, object]]:
    """Pick and validate the parser. A field the schema does not declare is an `extra.*` rule,
    whose value passes through as it is."""
    produces, default = (
        ("any", "raw") if field_spec is None else _PRODUCES[field_spec.nested or field_spec.kind]
    )
    if field_spec is not None and field_spec.parser is not None:
        default = field_spec.parser
    parser_name = body.get("parse", default)
    spec = PARSERS.get(parser_name) if isinstance(parser_name, str) else None
    if spec is None:
        raise ConfigError(f"{label}: unknown parser {parser_name!r} (known: {sorted(PARSERS)})")
    if produces != "any" and spec.produces != produces:
        raise ConfigError(f"{label}: parser {parser_name!r} does not produce a {produces} value")
    allowed = _COMMON_KEYS | spec.options
    stray = sorted(set(body) - allowed)
    if stray:
        raise ConfigError(f"{label}: option(s) {stray} do not apply to parser {parser_name!r}")
    missing = sorted(spec.required - set(body))
    if missing:
        raise ConfigError(f"{label}: parser {parser_name!r} needs {missing}")
    options = {k: v for k, v in body.items() if k in spec.options}
    _check_option_values(options, label)
    if parser_name == "composition":
        options = {"table": _code_table(options, label, base_dir, tables)}
    return str(parser_name), options


def _check_option_values(options: dict[str, object], label: str) -> None:
    flag = options.get("zero_is_missing")
    if flag is not None and not isinstance(flag, bool):
        raise ConfigError(f"{label}: 'zero_is_missing' must be true or false")
    currency = options.get("currency")
    if currency is not None and not (isinstance(currency, str) and _CURRENCY.fullmatch(currency)):
        raise ConfigError(f"{label}: 'currency' must be a 3-letter upper-case code")
    separators = options.get("separators")
    if separators is not None and not (isinstance(separators, str) and separators):
        raise ConfigError(f"{label}: 'separators' must be a non-empty string of characters")


def _code_table(
    options: dict[str, object], label: str, base_dir: Path, tables: list[CodeTable]
) -> CodeTable:
    scope, table_file = options.get("scope"), options.get("table_file")
    if (scope is None) == (table_file is None):
        raise ConfigError(
            f"{label}: a composition rule needs exactly one of 'scope' (an installed plugin's "
            "code table) or 'table_file' (a YAML file next to this mapping)"
        )
    if isinstance(scope, str):
        table = table_for(scope, "composition")
    elif isinstance(table_file, str):
        table = load_table_file(base_dir / table_file, kind="composition")
    else:
        raise ConfigError(f"{label}: 'scope' and 'table_file' must be strings")
    tables.append(table)
    return table


def _sources(raw: object, label: str) -> tuple[SourceRef, ...]:
    listed = [raw] if isinstance(raw, str) else raw
    if not isinstance(listed, list) or not listed:
        raise ConfigError(f"{label}: 'from' must be a path such as list.name, or a list of them")
    return tuple(_source_ref(item, label) for item in listed)


def _source_ref(item: object, label: str) -> SourceRef:
    match = _SOURCE_REF.fullmatch(item) if isinstance(item, str) else None
    if match is None:
        raise ConfigError(
            f"{label}: {item!r} is not 'list.<path>', 'detail.<path>' or 'context.<path>' "
            "(path like a.b[0].c)"
        )
    return SourceRef(cast("Scope", match["scope"]), match["path"])


def _image_rule(
    body: dict[str, object], label: str, on_null: OnNull, is_sensitive: bool
) -> FieldRule:
    stray = sorted(set(body) - _IMAGE_KEYS)
    if stray:
        raise ConfigError(f"{label}: option(s) {stray} do not apply to pictures")
    if ("roles" in body) == ("from" in body):
        raise ConfigError(f"{label}: give either 'from' (one picture) or 'roles' (several)")
    declared = body["roles"] if "roles" in body else {str(body.get("role", "thumb")): body["from"]}
    if not isinstance(declared, dict) or not declared:
        raise ConfigError(f"{label}: 'roles' must map a role name to a list of paths")
    roles: dict[str, tuple[SourceRef, ...]] = {}
    for role, where in declared.items():
        if not (isinstance(role, str) and _ROLE.fullmatch(role)):
            raise ConfigError(f"{label}: picture role {role!r} must match {_ROLE.pattern}")
        roles[role] = _sources(where, f"{label} role {role!r}")
    return FieldRule(
        sources=tuple(ref for refs in roles.values() for ref in refs),
        parser=None,
        options={},
        on_null=on_null,
        is_sensitive=is_sensitive,
        roles=roles,
    )


def _check_extra_rules_agree(fields: Mapping[str, FieldRule], name: str) -> None:
    """All extra.* rules write the one `extra` field, so they must agree on how it is loaded."""
    modes = {rule.on_null for key, rule in fields.items() if key.startswith("extra.")}
    if len(modes) > 1:
        raise ConfigError(f"mapping {name}: every extra.* rule must use the same 'on_null'")
