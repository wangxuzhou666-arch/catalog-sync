"""Apply a mapping to one raw bundle and get one record of the mapping's schema.

Pure: no network, no clock, no filesystem; everything it needs arrives in the arguments. That is
what lets a changed mapping be re-run over the stored cache without contacting the service.

Rules the engine keeps, whatever the mapping says:

* Nothing is invented. A field the source did not fill stays ``None`` (or empty), and a value that
  cannot be understood becomes ``None`` plus an entry in the record's ``issues``.
* Issues name the field and the reason (``weight_gsm:source_zero``, ``price:unparseable``,
  ``composition:unknown_code:X``), never the raw value, except for a code the table lacks.
* List and detail are read side by side, never merged: for each rule the first listed source that
  has a usable value wins, and an empty value in one payload cannot hide a value in the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from catalog_sync.http import strip_query
from catalog_sync.models import Composition, ImageRef, Money, Record, new_record
from catalog_sync.normalize.mapping import FieldRule, MappingSpec
from catalog_sync.normalize.parsers import PARSERS, ParseFailure
from catalog_sync.schema import NestedKind
from catalog_sync.sources.base import RawBundle, StoredAsset

_LIST_VALUED = frozenset({NestedKind.TEXTS, NestedKind.COMPOSITION, NestedKind.IMAGES})


@dataclass(frozen=True)
class Provenance:
    """Where a record's raw material came from; supplied by the caller, who knows the cache."""

    source_version: str | None
    raw_ref: str | None
    fetched_at: datetime | None
    normalized_at: datetime


@dataclass(frozen=True)
class _Selected:
    value: object  # None when no source produced a usable value
    issues: tuple[str, ...]


def normalize(bundle: RawBundle, spec: MappingSpec, provenance: Provenance) -> Record:
    values: dict[str, object] = {}
    extra: dict[str, dict[str, object]] = {}
    issues: list[str] = []
    for key, rule in spec.fields.items():
        if rule.is_picture:
            values[key] = _images(rule, bundle, issues)
            continue
        selected = _select(key, rule, bundle)
        issues.extend(selected.issues)
        if key.startswith("extra."):
            _, namespace, name = key.split(".")
            if selected.value is not None:
                extra.setdefault(namespace, {})[name] = _json_form(selected.value)
        else:
            values[key] = selected.value
    if "extra" in spec.record_fields:
        values["extra"] = extra
    if bundle.detail is None and spec.uses_detail:
        issues.append("detail:missing")
    return new_record(
        spec.schema,
        spec.source,
        bundle.item.source_id,
        {
            **_empty_for_none(values, spec),
            "issues": tuple(dict.fromkeys(issues)),
            "source_version": provenance.source_version,
            "raw_ref": provenance.raw_ref,
            "fetched_at": provenance.fetched_at,
            "normalized_at": provenance.normalized_at,
        },
    )


def _empty_for_none(values: dict[str, object], spec: MappingSpec) -> dict[str, object]:
    """A list-valued field with nothing to say is empty, not None. Fields the mapping does not
    own are left out, so the record gives them their defaults."""
    kept: dict[str, object] = {}
    for name, value in values.items():
        column = spec.schema.spec(name)
        is_list = column is not None and column.nested in _LIST_VALUED
        kept[name] = (value or ()) if is_list else value
    return kept


# --- one rule -----------------------------------------------------------------------------------


def _select(key: str, rule: FieldRule, bundle: RawBundle) -> _Selected:
    parser = PARSERS[str(rule.parser)]
    leftover: list[str] = []
    zero_seen = parse_failed = False
    for ref in rule.sources:
        raw = ref.read(bundle)
        if _is_blank(raw):
            continue
        try:
            parsed = parser.run(raw, rule.options)
        except ParseFailure:
            parse_failed = True
            continue
        if _is_blank(parsed.value):
            leftover.extend(parsed.issues)
        elif rule.is_zero_missing and _is_zero(parsed.value):
            zero_seen = True
        else:
            return _Selected(parsed.value, parsed.issues)
    if zero_seen:
        leftover.append(f"{key}:source_zero")
    if parse_failed:
        leftover.append(f"{key}:unparseable")
    return _Selected(None, tuple(leftover))


def _is_blank(value: object) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return value is None or value == [] or value == {} or value == ()


def _is_zero(value: object) -> bool:
    if isinstance(value, Money):
        return value.amount == Decimal(0)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _json_form(value: object) -> object:
    if isinstance(value, Money | Composition):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_json_form(item) for item in value]
    return value


# --- pictures -----------------------------------------------------------------------------------


def _images(rule: FieldRule, bundle: RawBundle, issues: list[str]) -> tuple[ImageRef, ...]:
    stored = {asset.ref.role: asset for asset in bundle.assets}
    images: list[ImageRef] = []
    for role, refs in rule.roles.items():
        url, was_rejected = _first_url([ref.read(bundle) for ref in refs])
        asset = stored.get(role)
        if url is None and asset is not None:
            url = asset.ref.url
        if url is None and was_rejected:
            issues.append(f"images:{role}:not_a_url")
        if url is not None or asset is not None:
            images.append(_image_ref(role, url, asset))
        if asset is not None and asset.is_unreadable:
            issues.append(f"images:{role}:unreadable")
    return tuple(images)


def _first_url(candidates: Sequence[object]) -> tuple[str | None, bool]:
    """The first candidate that is an http(s) URL, query stripped; and whether one was refused."""
    was_rejected = False
    for candidate in candidates:
        if _is_blank(candidate):
            continue
        if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
            return strip_query(candidate), was_rejected
        was_rejected = True
    return None, was_rejected


def _image_ref(role: str, url: str | None, asset: StoredAsset | None) -> ImageRef:
    if asset is None:
        return ImageRef(role=role, source_url=url)
    return ImageRef(
        role=role,
        source_url=url,
        local_path=f"assets/{asset.name}",
        sha256=asset.sha256,
        width=asset.width,
        height=asset.height,
        size_bytes=asset.size_bytes,
    )
