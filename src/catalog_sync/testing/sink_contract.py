"""Reusable contract suite for sinks.

Any sink, in this repo or in a private package, proves it honours the partial-upsert contract
(see ``catalog_sync.sinks.base``) by supplying a harness factory and running the checks::

    def make_harness(workdir: Path) -> SinkHarness: ...

    @pytest.mark.parametrize("check", CONTRACT_CHECKS, ids=lambda c: c.__name__)
    def test_contract(check, tmp_path):
        check(make_harness, tmp_path)

The suite is field-agnostic. Its checks need a field of each storage kind to write (text, real,
integer, a JSON object, a JSON list, a timestamp), and a ``ContractShape`` says which fields those
are. The default shape invents six such fields (``CONTRACT_KINDS``); ``shape_of(schema)`` derives
one from an entity schema, so the same checks run against the columns of the ``material`` schema,
of ``supplier``, or of your own, with every column of the schema written and read back in the
insert check. A harness names its shape (``SinkHarness.shape``); one that does not gets the
default.

Each check builds its own fresh sink in its own sub-directory, so checks never share state. A
failing check raises AssertionError with the violated invariant in the message.

Sink-specific hazards the generic suite cannot reach, such as foreign-key children surviving a
re-upsert in SQL sinks, belong in that sink's own tests.

A sink that stores nothing itself can declare it (``backup_supported = False``,
``dry_run_predicts_counts = False``, see ``catalog_sync.sinks.base``). The two checks that read
rows back from a backup, or counts from a dry run, then check the *declaration* instead: the backup
receipt must say in a note that no copy was taken (and hold no path, table or row count), and a
dry run must report no counts and explain itself in ``WriteReport.notes``. Every other check runs
unchanged: the stored rows are read through ``SinkHarness.read_rows`` whatever the sink is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from catalog_sync.errors import SinkError
from catalog_sync.models import RecordKey
from catalog_sync.schema import EntitySchema, FieldKind, FieldSpec, NestedKind
from catalog_sync.sinks.base import (
    BackupReceipt,
    Batch,
    BatchOrigin,
    Row,
    Sink,
    WriteReport,
    row_key,
)

Rows = Mapping[RecordKey, Mapping[str, object]]

CONTRACT_KINDS: Mapping[str, FieldKind] = {
    "name": FieldKind.TEXT,
    "weight": FieldKind.REAL,
    "rank": FieldKind.INTEGER,
    "price": FieldKind.JSON,
    "tags": FieldKind.JSON,
    "seen_at": FieldKind.TIMESTAMP,
}

CONTRACT_RUN_ID = "contract-run"
_PRICE_10 = {"amount": "10", "currency": "USD"}
_PRICE_11 = {"amount": "11", "currency": "USD"}
# UTC on purpose: a store that keeps real timestamps hands back the same instant in UTC, so only
# a UTC input reads back as the identical string.
_SEEN = "2026-01-02T03:04:05+00:00"
_ROLES = ("name", "weight", "rank", "price", "tags", "seen_at")


@dataclass(frozen=True)
class ContractShape:
    """The fields the checks write, by the role each plays.

    ``fields`` maps a role to a field name (``name`` is a text field, ``price`` a JSON object,
    ``tags`` a JSON list of strings, ...). ``samples`` holds a non-null value for *every* field
    in ``kinds``, which the insert check writes so that each column of a schema is exercised.
    ``objects`` are two different values the ``price`` field can hold.
    """

    label: str
    kinds: Mapping[str, FieldKind]
    fields: Mapping[str, str]
    samples: Mapping[str, object]
    objects: tuple[Mapping[str, object], Mapping[str, object]] = (_PRICE_10, _PRICE_11)

    def names(self, *roles: str) -> frozenset[str]:
        return frozenset(self.fields[role] for role in roles)

    @property
    def all_names(self) -> frozenset[str]:
        return frozenset(self.kinds)


GENERIC_SHAPE = ContractShape(
    label="generic",
    kinds=CONTRACT_KINDS,
    fields={role: role for role in _ROLES},
    samples={
        "name": "Alpha",
        "weight": 150.5,
        "rank": 3,
        "price": _PRICE_10,
        "tags": ["x", "y"],
        "seen_at": _SEEN,
    },
)


def shape_of(schema: EntitySchema) -> ContractShape:
    """A shape whose fields are the columns of `schema` (identity left out, as sinks key rows).

    The first column of each kind plays that kind's role; a schema with no declared integer or
    timestamp falls back on its provenance columns, which every schema has.
    """
    columns = schema.columns
    objects = [c for c in columns if c.nested in (NestedKind.MONEY, NestedKind.OBJECT)]
    chosen = {
        "name": _first(columns, lambda c: c.kind is FieldKind.TEXT),
        "weight": _first(columns, lambda c: c.kind is FieldKind.REAL),
        "rank": _first(columns, lambda c: c.kind is FieldKind.INTEGER),
        "price": objects[0],
        "tags": _first(columns, lambda c: c.nested is NestedKind.TEXTS),
        "seen_at": _first(columns, lambda c: c.kind is FieldKind.TIMESTAMP),
    }
    is_money = chosen["price"].nested is NestedKind.MONEY
    return ContractShape(
        label=schema.entity,
        kinds=schema.kinds,
        fields={role: spec.name for role, spec in chosen.items()},
        samples={spec.name: _sample(spec) for spec in columns},
        objects=(_PRICE_10, _PRICE_11) if is_money else ({"k": "one"}, {"k": "two"}),
    )


def _first(columns: tuple[FieldSpec, ...], wanted: Callable[[FieldSpec], bool]) -> FieldSpec:
    return next(spec for spec in columns if wanted(spec))


def _sample(spec: FieldSpec) -> object:
    """A non-null JSON value that fits the column, the same on every run."""
    nested = {
        NestedKind.TEXTS: ["a", "b"],
        NestedKind.MONEY: _PRICE_10,
        NestedKind.COMPOSITION: [{"fiber": "Cotton", "percent": 60.0, "raw": "60%C"}],
        NestedKind.IMAGES: [
            {
                "role": "thumb",
                "source_url": "https://assets.example.invalid/a.png",
                "local_path": None,
                "sha256": None,
                "width": 8,
                "height": 8,
                "size_bytes": None,
            }
        ],
        NestedKind.OBJECT: {"k": "v"},
    }
    scalar = {
        FieldKind.TEXT: f"text of {spec.name}",
        FieldKind.INTEGER: 7,
        FieldKind.REAL: 2.5,
        FieldKind.TIMESTAMP: _SEEN,
    }
    return nested[spec.nested] if spec.nested is not None else scalar[spec.kind]


@dataclass(frozen=True)
class SinkHarness:
    """A sink plus the two read-back functions the contract needs.

    Rows come back as dictionaries of JSON values (lists decoded, null as None), so every sink is
    compared on one vocabulary.
    """

    sink: Sink
    read_rows: Callable[[], Rows]
    read_backup: Callable[[BackupReceipt], Rows]
    shape: ContractShape = field(default=GENERIC_SHAPE)


HarnessFactory = Callable[[Path], SinkHarness]
ContractCheck = Callable[[HarnessFactory, Path], None]


def contract_row(
    source_id: str,
    *,
    source: str = "contract",
    name: str | None = None,
    weight: float | None = None,
    rank: int | None = None,
    price: Mapping[str, object] | None = None,
    tags: tuple[str, ...] = (),
    seen_at: str | None = None,
    shape: ContractShape = GENERIC_SHAPE,
    fill_others: bool = False,
) -> Row:
    """A row with the six role fields set as given. With `fill_others`, every other field of the
    shape holds its sample value instead of null."""
    roles = {
        "name": name,
        "weight": weight,
        "rank": rank,
        "price": price,
        "tags": list(tags),
        "seen_at": seen_at,
    }
    row: dict[str, object] = {"source": source, "source_id": source_id}
    if fill_others:
        row.update(shape.samples)
    else:
        row.update(dict.fromkeys(shape.kinds))
    row.update({shape.fields[role]: value for role, value in roles.items()})
    return row


def contract_batch(
    rows: list[Row],
    *,
    owned: frozenset[str] | None = None,
    keep_existing_if_null: frozenset[str] = frozenset(),
    is_full_snapshot: bool = False,
    shape: ContractShape = GENERIC_SHAPE,
) -> Batch:
    return Batch(
        rows,
        owned=shape.all_names if owned is None else owned,
        kinds=shape.kinds,
        keep_existing_if_null=keep_existing_if_null,
        is_full_snapshot=is_full_snapshot,
        origin=BatchOrigin(shape.label, 1, CONTRACT_RUN_ID),  # for the sinks that forward it
    )


def _start(make: HarnessFactory, workdir: Path, label: str) -> SinkHarness:
    harness = make(workdir / label)
    harness.sink.open()
    harness.sink.backup()
    return harness


def check_insert_roundtrip(make: HarnessFactory, workdir: Path) -> None:
    harness = _start(make, workdir, "insert")
    shape = harness.shape
    row = contract_row(
        "A",
        name="Alpha",
        weight=150.5,
        rank=3,
        price=shape.objects[0],
        tags=("x", "y"),
        seen_at=_SEEN,
        shape=shape,
        fill_others=True,
    )
    report = harness.sink.upsert(contract_batch([row], shape=shape))
    assert (report.inserted, report.updated, report.unchanged) == (1, 0, 0), report
    stored = harness.read_rows()[row_key(row)]
    assert {name: stored[name] for name in row} == dict(row), (
        "a stored row must read back as the row that was written, one value of every kind"
    )


def check_unowned_fields_untouched(make: HarnessFactory, workdir: Path) -> None:
    harness = _start(make, workdir, "unowned")
    shape = harness.shape
    f = shape.fields
    first = contract_row(
        "A", name="Old", weight=1.5, price=shape.objects[0], tags=("t",), shape=shape
    )
    harness.sink.upsert(contract_batch([first], shape=shape))
    key = row_key(first)
    # price, weight and tags are not owned by this batch: neither an incoming null nor a value
    # may touch them.
    nulls = contract_row("A", name="New", weight=None, price=None, tags=(), shape=shape)
    harness.sink.upsert(contract_batch([nulls], owned=shape.names("name"), shape=shape))
    stored = harness.read_rows()[key]
    assert stored[f["name"]] == "New", "owned field must be updated"
    assert stored[f["price"]] == shape.objects[0], (
        "a field the batch does not own must stay as it was"
    )
    assert stored[f["weight"]] == 1.5, "a number the batch does not own must stay as it was"
    assert stored[f["tags"]] == ["t"], "a list the batch does not own must stay as it was"

    # Absent is not null: a row that does not even carry the unowned fields changes nothing.
    sparse: Row = {"source": "contract", "source_id": "A", f["name"]: "Newer"}
    harness.sink.upsert(contract_batch([sparse], owned=shape.names("name"), shape=shape))
    stored = harness.read_rows()[key]
    assert stored[f["name"]] == "Newer"
    assert stored[f["price"]] == shape.objects[0], "an absent field must not be read as null"


def check_owned_null_writes_null(make: HarnessFactory, workdir: Path) -> None:
    harness = _start(make, workdir, "owned_null")
    shape = harness.shape
    f = shape.fields
    harness.sink.upsert(
        contract_batch(
            [contract_row("A", name="Old", price=shape.objects[0], rank=2, shape=shape)],
            shape=shape,
        )
    )
    cleared = contract_row("A", name="Old", price=None, rank=None, shape=shape)
    harness.sink.upsert(
        contract_batch([cleared], owned=shape.names("name", "price", "rank"), shape=shape)
    )
    stored = harness.read_rows()[row_key(cleared)]
    assert stored[f["price"]] is None, "an owned field with a null value must be written as null"
    assert stored[f["rank"]] is None


def check_keep_existing_if_null(make: HarnessFactory, workdir: Path) -> None:
    harness = _start(make, workdir, "keep_existing")
    shape = harness.shape
    f = shape.fields
    harness.sink.upsert(
        contract_batch(
            [contract_row("A", price=shape.objects[0], tags=("t",), weight=2.5, shape=shape)],
            shape=shape,
        )
    )
    owned = shape.names("name", "price", "tags", "weight")
    keep = shape.names("price", "tags", "weight")

    gap = contract_row("A", name="Named", price=None, tags=(), weight=None, shape=shape)
    harness.sink.upsert(contract_batch([gap], owned=owned, keep_existing_if_null=keep, shape=shape))
    stored = harness.read_rows()[row_key(gap)]
    assert stored[f["price"]] == shape.objects[0], "null must not overwrite a kept field"
    assert stored[f["tags"]] == ["t"], "an empty list counts as null for kept fields"
    assert stored[f["weight"]] == 2.5, "null must not overwrite a kept number"
    assert stored[f["name"]] == "Named", "fields that are not kept still take the incoming value"

    fresh = contract_row(
        "A", name="Named", price=shape.objects[1], tags=("u",), weight=3.5, shape=shape
    )
    harness.sink.upsert(
        contract_batch([fresh], owned=owned, keep_existing_if_null=keep, shape=shape)
    )
    stored = harness.read_rows()[row_key(fresh)]
    assert stored[f["price"]] == shape.objects[1], (
        "a non-null incoming value must replace a kept one"
    )
    assert stored[f["tags"]] == ["u"]
    assert stored[f["weight"]] == 3.5


def check_never_deletes_rows(make: HarnessFactory, workdir: Path) -> None:
    harness = _start(make, workdir, "no_delete")
    shape = harness.shape
    first = contract_row("A", name="a", shape=shape)
    second = contract_row("B", name="b", shape=shape)
    harness.sink.upsert(contract_batch([first, second], shape=shape))

    report = harness.sink.upsert(contract_batch([first], is_full_snapshot=True, shape=shape))
    assert report.absent == (row_key(second),), "a row the source no longer returns is reported"
    assert row_key(second) in harness.read_rows(), "a reported row must not be deleted"

    report = harness.sink.upsert(contract_batch([first], shape=shape))
    assert report.absent == (), "an incremental batch says nothing about rows it does not contain"


def check_idempotent(make: HarnessFactory, workdir: Path) -> None:
    harness = _start(make, workdir, "idempotent")
    shape = harness.shape
    batch = contract_batch(
        [
            contract_row("A", name="a", weight=1.5, price=shape.objects[0], shape=shape),
            contract_row("B", name="b", tags=("q",), shape=shape),
        ],
        shape=shape,
    )
    harness.sink.upsert(batch)
    rows_after_first = dict(harness.read_rows())
    report = harness.sink.upsert(batch)
    assert (report.inserted, report.updated, report.unchanged) == (0, 0, 2), report
    assert dict(harness.read_rows()) == rows_after_first, "re-applying a batch must change nothing"


def check_key_includes_source(make: HarnessFactory, workdir: Path) -> None:
    harness = _start(make, workdir, "keys")
    shape = harness.shape
    one = contract_row("A", source="s1", name="from one", shape=shape)
    two = contract_row("A", source="s2", name="from two", shape=shape)
    harness.sink.upsert(contract_batch([one, two], shape=shape))
    rows = harness.read_rows()
    name = shape.fields["name"]
    assert rows[row_key(one)][name] == "from one"
    assert rows[row_key(two)][name] == "from two", "one source_id under two sources is two rows"


def check_backup_precedes_upsert(make: HarnessFactory, workdir: Path) -> None:
    harness = make(workdir / "backup")
    shape = harness.shape
    name = shape.fields["name"]
    row = partial(contract_row, shape=shape)
    batch = partial(contract_batch, shape=shape)
    harness.sink.open()
    try:
        harness.sink.upsert(batch([row("A")]))
    except SinkError:
        pass
    else:
        raise AssertionError("upsert() without a preceding backup() must be refused")
    receipt = harness.sink.backup()
    if not harness.sink.backup_supported:
        _check_declared_no_backup(receipt)
        harness.sink.upsert(batch([row("A", name="before")]))
        harness.sink.close()
        return
    harness.sink.upsert(batch([row("A", name="before")]))
    harness.sink.close()

    harness.sink.open()
    receipt = harness.sink.backup()
    assert receipt.rows_before == 1, receipt
    harness.sink.upsert(batch([row("A", name="after"), row("B")]))
    backed_up = harness.read_backup(receipt)
    assert set(backed_up) == {("contract", "A")}, "the backup must hold exactly the pre-load rows"
    assert backed_up[("contract", "A")][name] == "before", "the backup must not follow later writes"


def _check_declared_no_backup(receipt: BackupReceipt) -> None:
    """A sink that takes no backup must not look as if it had: no file, no table, no row count,
    and a note that says so (and whose job recovery is)."""
    assert receipt.path is None and receipt.table is None, "a sink without backups names no copy"
    assert receipt.rows_before == 0, "a sink without backups cannot know the rows it had before"
    assert receipt.note, "a sink without backups must say so in the receipt's note"


def check_dry_run_writes_nothing(make: HarnessFactory, workdir: Path) -> None:
    harness = make(workdir / "dry_run")
    shape = harness.shape
    row = partial(contract_row, shape=shape)
    batch = partial(contract_batch, shape=shape)
    harness.sink.open()
    new = batch([row("A", name="a", price=shape.objects[0])])

    # Before any backup: a dry run is allowed, reports what a load would do, and stores nothing.
    report = harness.sink.upsert(new, dry_run=True)
    _check_dry_run_report(harness.sink, report, (1, 0, 0))
    assert dict(harness.read_rows()) == {}, "a dry run must not create rows"
    assert list(workdir.rglob("*.bak*")) == [], "a dry run must not take a backup"

    harness.sink.backup()
    harness.sink.upsert(new)
    stored = dict(harness.read_rows())
    changed = batch(
        [row("A", name="changed", price=shape.objects[0]), row("B", name="b")],
        is_full_snapshot=True,
    )
    report = harness.sink.upsert(changed, dry_run=True)
    _check_dry_run_report(harness.sink, report, (1, 1, 0))
    assert dict(harness.read_rows()) == stored, "a dry run must leave existing rows as they were"

    real = harness.sink.upsert(changed)
    assert (real.inserted, real.updated, real.unchanged) == (1, 1, 0), (
        "a dry run must predict what the real load then does"
    )


def _check_dry_run_report(sink: Sink, report: WriteReport, expected: tuple[int, int, int]) -> None:
    counts = (report.inserted, report.updated, report.unchanged)
    if sink.dry_run_predicts_counts:
        assert counts == expected, report
        return
    assert counts == (0, 0, 0), "a sink that cannot predict must not invent counts"
    assert report.notes, "a sink that cannot predict must say so in the report's notes"


CONTRACT_CHECKS: tuple[ContractCheck, ...] = (
    check_insert_roundtrip,
    check_unowned_fields_untouched,
    check_owned_null_writes_null,
    check_keep_existing_if_null,
    check_never_deletes_rows,
    check_idempotent,
    check_key_includes_source,
    check_backup_precedes_upsert,
    check_dry_run_writes_nothing,
)


def run_contract(make: HarnessFactory, workdir: Path) -> None:
    """Run every check. Convenience for callers that do not use pytest parametrisation."""
    for check in CONTRACT_CHECKS:
        check(make, workdir)
