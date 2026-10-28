"""Reading what a load left behind, and comparing it: store with store, and store with the oracle.

The oracle is the seeded Mock catalogue of the repository (``tests.fixtures.mock_saas``): the
expected row counts and the expected record of an id come from the same dataset the installed Mock
source served, never from a number typed in here. This module imports repository code; the commands
under test do not (they run from the installed copy).
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from acceptance_lab import MATERIAL_COUNT, SEED, SUPPLIER_COUNT, Entity
from acceptance_report import MAX_SHOWN_MISMATCHES

Key = tuple[str, str]  # (source, source_id)
Record = dict[str, object]
SAMPLE_STEP = 3  # the fixed sample: every third id, and the last


def read_jsonl(path: Path) -> dict[Key, Record]:
    records: dict[Key, Record] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[(str(record["source"]), str(record["source_id"]))] = record
    return records


def read_sidecar(normalized: Path) -> dict[str, object]:
    """The ``F.jsonl.meta.json`` next to a normalized file."""
    sidecar = normalized.with_name(normalized.name + ".meta.json")
    loaded: dict[str, object] = json.loads(sidecar.read_text(encoding="utf-8"))
    return loaded


def read_sqlite(path: Path, table: str) -> dict[Key, Record]:
    from catalog_sync.sinks.sqlite import read_table  # the repository's own reader

    return dict(read_table(path, table))


def read_postgres(dsn: str, table: str, schema: str) -> dict[Key, Record]:
    """Every row of `schema.table` in Postgres as JSON values (the repository's own reader)."""
    from catalog_sync.sinks.postgres import read_table

    return dict(read_table(dsn, table, schema))


@contextlib.contextmanager
def postgres_schema(dsn: str, schema: str) -> Iterator[None]:
    """A schema of its own for one run, dropped (with whatever the run put in it) afterwards."""
    from catalog_sync.sinks.postgres import connect

    conn = connect(dsn)
    try:
        conn.execute(f'CREATE SCHEMA "{schema}"')  # the name is the harness's own: acc_<hex>
        try:
            yield
        finally:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        conn.close()


def owned_fields(sidecar: Mapping[str, object]) -> list[str]:
    """What a load wrote: the sidecar's owned fields, minus the ones a drop policy withholds."""
    owned = sidecar["owned"]
    sensitive = sidecar.get("sensitive", [])
    assert isinstance(owned, list) and isinstance(sensitive, list)
    return sorted(str(name) for name in owned if name not in sensitive)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


@dataclass
class FieldComparison:
    stores: tuple[str, ...]
    records: int = 0
    fields: int = 0
    mismatches: list[dict[str, object]] = field(default_factory=list)

    @property
    def is_equal(self) -> bool:
        return not self.mismatches

    def evidence(self) -> dict[str, object]:
        return {
            "stores": list(self.stores),
            "records": self.records,
            "field_values_compared": self.fields,
            "mismatch_count": len(self.mismatches),
            "mismatches_shown": self.mismatches[:MAX_SHOWN_MISMATCHES],
        }


def compare_stores(
    stores: Mapping[str, Mapping[Key, Record]], reference: str, fields: Sequence[str]
) -> FieldComparison:
    """Every store against the reference: the same ids, and the same value in each owned field."""
    comparison = FieldComparison(tuple(stores))
    base = stores[reference]
    comparison.records = len(base)
    for name, other in stores.items():
        if name == reference:
            continue
        _compare_ids(comparison, name, base, other)
        for key in sorted(base.keys() & other.keys()):
            _compare_fields(comparison, name, key, base[key], other[key], fields)
    return comparison


def _compare_ids(
    comparison: FieldComparison, name: str, base: Mapping[Key, Record], other: Mapping[Key, Record]
) -> None:
    for key in sorted(base.keys() - other.keys()):
        comparison.mismatches.append({"store": name, "id": key[1], "problem": "missing"})
    for key in sorted(other.keys() - base.keys()):
        comparison.mismatches.append({"store": name, "id": key[1], "problem": "extra"})


def _compare_fields(
    comparison: FieldComparison,
    name: str,
    key: Key,
    expected: Record,
    found: Record,
    fields: Sequence[str],
) -> None:
    for field_name in fields:
        comparison.fields += 1
        if canonical(expected.get(field_name)) != canonical(found.get(field_name)):
            comparison.mismatches.append(
                {
                    "store": name,
                    "id": key[1],
                    "field": field_name,
                    "reference": expected.get(field_name),
                    "found": found.get(field_name),
                }
            )


# --- looking for values where they must not be ---------------------------------------------------


def tree_blobs(root: Path, place: str) -> Iterator[tuple[str, bytes]]:
    """(place, bytes) of every file under `root`; the place is `place/<top-level entry>`, so a
    directory with thousands of files is one place."""
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield f"{place}/{path.relative_to(root).parts[0]}", path.read_bytes()


def find_values(
    needles: Iterable[tuple[str, str]], blobs: Iterable[tuple[str, bytes]]
) -> dict[tuple[str, str], int]:
    """Where each kind of value occurs: (kind, place) -> how many distinct values of that kind the
    place holds. `needles` are (kind, value); values are matched as UTF-8 text in the bytes (a
    SQLite file keeps its text as it is, so a value in a row is found in the file)."""
    found: dict[tuple[str, str], set[str]] = defaultdict(set)
    needle_list = list(needles)
    for place, data in blobs:
        for kind, value in needle_list:
            if value.encode("utf-8") in data:
                found[(kind, place)].add(value)
    return {key: len(values) for key, values in found.items()}


# --- the oracle ---------------------------------------------------------------------------------


class Oracle:
    """The seeded Mock catalogue, and the record a correct run must have stored for each id."""

    def __init__(self, repo: Path) -> None:
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))  # makes `tests.fixtures` importable from a script
        from catalog_sync.mock.data import generate

        self.dataset = generate(SEED, materials=MATERIAL_COUNT, suppliers=SUPPLIER_COUNT)

    def ids(self, entity: Entity) -> list[str]:
        if entity.name == "materials":
            return [material.id for material in self.dataset.materials]
        return [supplier.id for supplier in self.dataset.suppliers]

    def source_attribute(self, entity: Entity, source_id: str, name: str) -> object:
        """A field of the seeded catalogue's own record (what the Mock source serves), by name."""
        if entity.name == "materials":
            return getattr(self.dataset.material(source_id), name, None)
        supplier = next(s for s in self.dataset.suppliers if s.id == source_id)
        return getattr(supplier, name, None)

    def expected(self, entity: Entity, source_id: str, base_url: str) -> Record:
        from tests.fixtures.mock_saas import expected_material, expected_supplier

        if entity.name == "materials":
            material = self.dataset.material(source_id)
            assert material is not None, source_id
            return dict(expected_material(base_url, self.dataset, material))
        supplier = next(s for s in self.dataset.suppliers if s.id == source_id)
        return dict(expected_supplier(supplier))


def sample_ids(ids: Sequence[str]) -> list[str]:
    """A fixed sample: every third id (from the first), and the last one."""
    ordered = sorted(ids)
    chosen = ordered[::SAMPLE_STEP]
    return chosen if ordered[-1] in chosen else [*chosen, ordered[-1]]


def oracle_mismatches(
    oracle: Oracle,
    entity: Entity,
    base_url: str,
    stores: Mapping[str, Mapping[Key, Record]],
    ids: Sequence[str],
) -> list[dict[str, object]]:
    """Where a store's record differs from the oracle's expected one, for these ids."""
    mismatches: list[dict[str, object]] = []
    rows_by_id = {
        name: {source_id: row for (_, source_id), row in rows.items()}
        for name, rows in stores.items()
    }
    for source_id in ids:
        expected = oracle.expected(entity, source_id, base_url)
        for name, rows in rows_by_id.items():
            if source_id not in rows:
                mismatches.append({"store": name, "id": source_id, "problem": "missing"})
                continue
            wrong = _differing_keys(rows[source_id], expected)
            if wrong:
                mismatches.append({"store": name, "id": source_id, "differs": wrong})
    return mismatches


def _differing_keys(row: Record, expected: Record) -> list[str]:
    """The keys of `expected` whose value in `row` is not the expected one."""
    from catalog_sync.testing.oracle import picked

    try:
        narrowed = picked(row, expected)
    except KeyError as missing:
        return [f"{missing.args[0]} (absent)"]
    return sorted(key for key in expected if canonical(narrowed[key]) != canonical(expected[key]))
