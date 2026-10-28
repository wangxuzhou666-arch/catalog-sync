"""The normalised file: records as JSON Lines, plus a small sidecar saying how to load them.

``normalize --out F.jsonl`` writes ``F.jsonl`` (one record per line) and ``F.jsonl.meta.json``.
The sidecar carries what a load needs and a bare record cannot say: the schema the records follow
(embedded whole, so a load needs nothing but these two files), which fields the mapping owns, which
of them keep an existing value when empty, which are sensitive, and whether the records are the
source's whole listing. Without it ``load`` refuses to run, because guessing the owned set would
make it overwrite fields nobody asked it to touch. A sidecar from before schemas existed has no
``schema`` entry and means ``material``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from catalog_sync.errors import ConfigError, RecordFormatError
from catalog_sync.fsutil import atomic_write_bytes
from catalog_sync.models import MATERIAL_SCHEMA, Record, record_class
from catalog_sync.normalize.mapping import MappingSpec
from catalog_sync.normalize.run import NormalizedRun
from catalog_sync.schema import EntitySchema, parse_schema

META_SUFFIX = ".meta.json"
_FORMAT = 1


@dataclass(frozen=True)
class NormalizedFile:
    schema: EntitySchema
    records: tuple[Record, ...]
    owned: frozenset[str]
    keep_existing_if_null: frozenset[str]
    sensitive: frozenset[str]
    is_full_snapshot: bool
    source: str
    run_id: str
    mapping_digest: str


def from_run(normalized: NormalizedRun, spec: MappingSpec) -> NormalizedFile:
    return NormalizedFile(
        schema=spec.schema,
        records=normalized.records,
        owned=spec.record_fields | spec.schema.loaded_provenance,
        keep_existing_if_null=spec.keep_existing_if_null,
        sensitive=frozenset(spec.sensitive_fields),
        is_full_snapshot=normalized.is_full_snapshot,
        source=spec.source,
        run_id=normalized.run.run_id,
        mapping_digest=spec.digest,
    )


def meta_path(path: Path) -> Path:
    return path.with_name(path.name + META_SUFFIX)


def write_normalized(path: Path, normalized: NormalizedFile) -> None:
    lines = [
        json.dumps(record.to_dict(), ensure_ascii=False, allow_nan=False)
        for record in normalized.records
    ]
    atomic_write_bytes(path, "".join(f"{line}\n" for line in lines).encode("utf-8"))
    meta = {
        "format": _FORMAT,
        "source": normalized.source,
        "run_id": normalized.run_id,
        "mapping_sha256": normalized.mapping_digest,
        "schema": normalized.schema.to_json(),
        "record_count": len(normalized.records),
        "owned": sorted(normalized.owned),
        "keep_existing_if_null": sorted(normalized.keep_existing_if_null),
        "sensitive": sorted(normalized.sensitive),
        "is_full_snapshot": normalized.is_full_snapshot,
    }
    atomic_write_bytes(meta_path(path), (json.dumps(meta, indent=2) + "\n").encode("utf-8"))


def read_normalized(path: Path) -> NormalizedFile:
    meta = _read_meta(meta_path(path))
    schema = _schema(meta, meta_path(path).name)
    records = _read_records(path, schema)
    if len(records) != meta["record_count"]:
        raise RecordFormatError(
            f"{path.name} holds {len(records)} record(s) but its metadata says "
            f"{meta['record_count']}: the file was edited or truncated after normalising"
        )
    return NormalizedFile(
        schema=schema,
        records=records,
        owned=frozenset(_names(meta, "owned")),
        keep_existing_if_null=frozenset(_names(meta, "keep_existing_if_null")),
        sensitive=frozenset(_names(meta, "sensitive")),
        is_full_snapshot=meta["is_full_snapshot"] is True,
        source=str(meta["source"]),
        run_id=str(meta["run_id"]),
        mapping_digest=str(meta["mapping_sha256"]),
    )


def _read_meta(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise RecordFormatError(
            f"{path.name} is missing: `load` needs the metadata `normalize` writes next to the "
            "records (which fields are owned, which are sensitive)"
        )
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecordFormatError(f"{path.name} is not valid JSON") from exc
    required = {"source", "run_id", "mapping_sha256", "record_count", "owned", "is_full_snapshot"}
    if not isinstance(meta, dict) or meta.get("format") != _FORMAT or required - set(meta):
        raise RecordFormatError(f"{path.name} is not metadata written by this version")
    return meta


def _schema(meta: dict[str, object], name: str) -> EntitySchema:
    embedded = meta.get("schema")
    if embedded is None:
        return MATERIAL_SCHEMA
    try:
        return parse_schema(embedded, f"embedded in {name}")
    except ConfigError as exc:
        raise RecordFormatError(f"{name} carries a schema that is not valid: {exc}") from exc


def _names(meta: dict[str, object], key: str) -> list[str]:
    names = meta.get(key, [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise RecordFormatError(f"metadata field {key!r} must be a list of names")
    return names


def _read_records(path: Path, schema: EntitySchema) -> tuple[Record, ...]:
    records: list[Record] = []
    record_type = record_class(schema)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecordFormatError(f"cannot read {path.name}: {exc.strerror}") from exc
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecordFormatError(f"{path.name} line {number} is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise RecordFormatError(f"{path.name} line {number} is not a JSON object")
        records.append(record_type.from_dict(raw))
    return tuple(records)
